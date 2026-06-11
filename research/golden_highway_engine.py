"""
Golden Highway Engine -- Phase 1A  (v2 — structure_engine calibrated)
======================================================================
Principle: MACRO DIRECTION -> BOS -> PULLBACK -> PULLBACK ENDS -> ENTER

Changes from v1:
  - Per-instrument fractal left/right/min_age/min_swing from structure_engine.py
  - BOS quality uses new 7-pillar scoring (replaces old 7-pillar version)
  - Session detection per bar (UTC hours -> TOKYO/LONDON/OVERLAP/NY/OFFHOURS)
  - Session validity gate at BOS time (per instrument entry rules)
  - JP225 Tokyo strict mode (min_bos=5 vs standard 4)
  - swing_origin tracked for Zone 4 pullback invalidation
  - Per-instrument min_end_score, sl_atr_mult, tp targets from structure_engine
  - 15m TF support added (JP225 15m, US30 15m, GBPUSD 15m, US30 15m)
  - HK50 re-added (flagged separately in output)
  - JP225 re-added across all TFs
"""

import io, sys, warnings
# Reconfigure encoding in-place — avoids closing the original stdout fd
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
elif hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
warnings.filterwarnings("ignore")

import pandas as pd
import numpy as np
from pathlib import Path

# ── Import calibration module ──────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).resolve().parent))
from structure_engine import (
    f_get_fractal_params,
    f_get_entry_rules,
    f_get_min_bos_for_session,
    f_check_session_valid,
    f_classify_bos,
    f_get_pullback_zone,
    f_check_pullback_end,
)

ROOT    = Path(__file__).resolve().parent.parent
PQ_DIR  = ROOT / "data" / "processed"
OUT_DIR = ROOT / "outputs"
OUT_DIR.mkdir(exist_ok=True)

# ── Fixed parameters (not instrument-specific) ─────────────────────────────────
RSI_LEN        = 14
ATR_LEN        = 14
MAX_HOLD       = 20        # max bars to hold any trade
MAX_STAGE_TIME = 150       # bars before auto-reset from stage 2/3
OOS_FRAC       = 0.20      # most recent fraction used for OOS
MIN_OOS_TRADES = 10        # minimum for non-flagged reporting
TP1_R          = 1.0       # TP1 always at 1:1

# ── Instrument list with timeframes and special flags ─────────────────────────
# Format: (instrument, timeframe, flag_string_or_None)
INSTRUMENT_TFS = [
    # Indices
    ("JP225",  "1H",  None),                     # PRIMARY — re-added to GH
    ("JP225",  "4H",  None),
    ("JP225",  "15m", "15m data from ~Mar 2026 only"),
    ("US30",   "1H",  None),
    ("US30",   "4H",  None),
    ("US30",   "15m", "15m data from ~Mar 2026 only"),
    ("USTEC",  "1H",  None),
    ("USTEC",  "4H",  None),
    ("DE40",   "1H",  None),
    ("UK100",  "1H",  None),
    ("HK50",   "1H",  "Was 6.7% ruin on H2 Markov — test GH separately"),
    # Forex
    ("GBPUSD", "1H",  None),
    ("GBPUSD", "15m", "15m data from ~Mar 2026 only"),
    ("EURUSD", "1H",  None),
    ("GBPJPY", "1H",  None),
    ("EURJPY", "1H",  None),
    ("USDJPY", "1H",  None),
    ("XAGUSD", "1H",  None),
    # Commodities
    ("XAUUSD", "1H",  None),
    ("XAUUSD", "4H",  None),
    # Previously included — keep for comparison
    ("USDCAD", "4H",  None),
    ("USDCAD", "1H",  None),
]

# ══════════════════════════════════════════════════════════════════════════════
# 1. CORE INDICATORS
# ══════════════════════════════════════════════════════════════════════════════

def rsi(series: pd.Series, n: int = 14) -> pd.Series:
    """Wilder's RSI via EWM."""
    delta    = series.diff()
    gain     = delta.clip(lower=0)
    loss     = (-delta).clip(lower=0)
    avg_gain = gain.ewm(com=n - 1, adjust=False).mean()
    avg_loss = loss.ewm(com=n - 1, adjust=False).mean()
    rs       = avg_gain / (avg_loss + 1e-10)
    return 100.0 - 100.0 / (1.0 + rs)


def atr_series(df: pd.DataFrame, n: int = 14) -> pd.Series:
    pc = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - pc).abs(),
        (df["low"]  - pc).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(com=n - 1, adjust=False).mean()


# ══════════════════════════════════════════════════════════════════════════════
# 2. PIVOT DETECTION  (asymmetric left / right)
# ══════════════════════════════════════════════════════════════════════════════

def pivot_highs(highs: np.ndarray, left: int = 5, right: int = 3) -> np.ndarray:
    """
    Pivot high at bar j: highs[j] > all j-left..j-1 AND all j+1..j+right.
    First usable (known) at bar j+right.  No lookahead.
    """
    N   = len(highs)
    out = np.full(N, np.nan)
    for j in range(left, N - right):
        h = highs[j]
        if (all(h > highs[j - k] for k in range(1, left + 1)) and
                all(h > highs[j + k] for k in range(1, right + 1))):
            out[j] = h
    return out


def pivot_lows(lows: np.ndarray, left: int = 5, right: int = 3) -> np.ndarray:
    N   = len(lows)
    out = np.full(N, np.nan)
    for j in range(left, N - right):
        l = lows[j]
        if (all(l < lows[j - k] for k in range(1, left + 1)) and
                all(l < lows[j + k] for k in range(1, right + 1))):
            out[j] = l
    return out


def last_confirmed_pivot(piv: np.ndarray, confirm: int = 3) -> np.ndarray:
    """
    At bar i: most recent pivot confirmed by bar i
    (pivot bar j is confirmed once i >= j + confirm).
    """
    N   = len(piv)
    out = np.full(N, np.nan)
    cur = np.nan
    for i in range(N):
        j = i - confirm
        if j >= 0 and not np.isnan(piv[j]):
            cur = piv[j]
        out[i] = cur
    return out


def last_confirmed_pivot_idx(piv: np.ndarray, confirm: int = 3) -> np.ndarray:
    """Like last_confirmed_pivot but returns the bar index of the pivot (not its value)."""
    N   = len(piv)
    out = np.full(N, np.nan)
    cur = np.nan
    for i in range(N):
        j = i - confirm
        if j >= 0 and not np.isnan(piv[j]):
            cur = float(j)
        out[i] = cur
    return out


def second_confirmed_pivot(piv: np.ndarray, confirm: int = 3) -> np.ndarray:
    """
    Second-to-last confirmed pivot (used for sweep detection).
    Returns the pivot value BEFORE the most recent confirmed one.
    """
    N   = len(piv)
    out = np.full(N, np.nan)
    prev, cur = np.nan, np.nan
    for i in range(N):
        j = i - confirm
        if j >= 0 and not np.isnan(piv[j]):
            prev = cur
            cur  = piv[j]
        out[i] = prev
    return out


# ══════════════════════════════════════════════════════════════════════════════
# 3. SESSION DETECTION
# ══════════════════════════════════════════════════════════════════════════════

def _hour_to_session(hour_utc: int) -> str:
    """
    Map UTC hour to session label.
    Tokyo:   00:00-06:59 UTC
    London:  07:00-12:59 UTC
    Overlap: 13:00-15:59 UTC  (London + NY)
    NY:      16:00-20:59 UTC
    OffHrs:  21:00-23:59 UTC
    """
    if 0  <= hour_utc <  7:  return "TOKYO"
    if 7  <= hour_utc < 13:  return "LONDON"
    if 13 <= hour_utc < 16:  return "OVERLAP"
    if 16 <= hour_utc < 21:  return "NY"
    return "OFFHOURS"


# ══════════════════════════════════════════════════════════════════════════════
# 4. FEATURE BUILDER
# ══════════════════════════════════════════════════════════════════════════════

def build_features(instrument: str, tf: str = "1H") -> pd.DataFrame | None:
    """
    Load raw OHLC parquet and compute all GH features.
    Returns DataFrame with integer index and all feature columns.
    Returns None if parquet file missing or too short.
    """
    base_path = PQ_DIR / f"H2_raw_{instrument}_{tf}.parquet"
    if not base_path.exists():
        return None

    df = (pd.read_parquet(base_path)
            .sort_values("datetime_utc")
            .reset_index(drop=True))
    if len(df) < 200:
        return None

    df["rsi_base"]  = rsi(df["close"], RSI_LEN)
    df["atr14"]     = atr_series(df, ATR_LEN)
    df["vol_avg20"] = df["volume"].rolling(20).mean()
    df["vol_ratio"] = df["volume"] / (df["vol_avg20"] + 1e-10)

    # ── Session label ─────────────────────────────────────────────────────────
    hours = pd.to_datetime(df["datetime_utc"]).dt.hour
    df["session"] = hours.map(_hour_to_session)

    # ── Helper: merge a TF parquet column onto df ─────────────────────────────
    def merge_tf(src_tf: str, feature_dict: dict) -> dict:
        p = PQ_DIR / f"H2_raw_{instrument}_{src_tf}.parquet"
        if not p.exists():
            return {}
        src = pd.read_parquet(p).sort_values("datetime_utc").reset_index(drop=True)
        result = {}
        for new_name, (src_col, fn) in feature_dict.items():
            src_ser = fn(src[src_col]) if fn else src[src_col]
            tmp = src[["datetime_utc"]].copy()
            tmp[new_name] = src_ser.values
            merged = pd.merge_asof(
                df[["datetime_utc"]].rename(columns={"datetime_utc": "ts"}),
                tmp.rename(columns={"datetime_utc": "ts"}),
                on="ts", direction="backward",
            )
            result[new_name] = merged[new_name].values
        return result

    # ── 4H features: RSI + EMA21/50/200 + HTF alignment ──────────────────────
    p4h = PQ_DIR / f"H2_raw_{instrument}_4H.parquet"
    if p4h.exists():
        s4h = pd.read_parquet(p4h).sort_values("datetime_utc").reset_index(drop=True)
        s4h["rsi_4h"]  = rsi(s4h["close"], RSI_LEN)
        s4h["ema21"]   = s4h["close"].ewm(span=21,  adjust=False).mean()
        s4h["ema50"]   = s4h["close"].ewm(span=50,  adjust=False).mean()
        s4h["ema200"]  = s4h["close"].ewm(span=200, adjust=False).mean()
        s4h["htf"]     = np.where(
            (s4h["ema21"] > s4h["ema50"]) & (s4h["ema50"] > s4h["ema200"]),  1,
            np.where(
            (s4h["ema21"] < s4h["ema50"]) & (s4h["ema50"] < s4h["ema200"]), -1, 0))

        r4h_m = pd.merge_asof(df[["datetime_utc"]].copy(),
                               s4h[["datetime_utc", "rsi_4h", "htf", "ema50"]],
                               on="datetime_utc", direction="backward")
        df["rsi_4h"]    = r4h_m["rsi_4h"].values.astype(float)
        df["htf_align"] = r4h_m["htf"].values.astype(float)
        df["ema50_4h"]  = r4h_m["ema50"].values.astype(float)
    else:
        df["rsi_4h"]    = np.nan
        df["htf_align"] = 0.0
        df["ema50_4h"]  = df["close"].values.astype(float)

    # ── Daily RSI ─────────────────────────────────────────────────────────────
    r1d = merge_tf("D", {"rsi_1d": ("close", lambda s: rsi(s, RSI_LEN))})
    df["rsi_1d"] = r1d.get("rsi_1d", np.full(len(df), np.nan))

    # ── 2H RSI (for Micro Completion) ─────────────────────────────────────────
    if tf in ("1H",):
        # Resample 1H to 2H
        tmp2h = (df.set_index("datetime_utc")["close"]
                   .resample("2h").last().dropna().reset_index())
        tmp2h["rsi_2h_val"] = rsi(tmp2h["close"], RSI_LEN).values
        df = pd.merge_asof(
            df.sort_values("datetime_utc"),
            tmp2h.rename(columns={"datetime_utc": "datetime_utc"})[["datetime_utc", "rsi_2h_val"]],
            on="datetime_utc", direction="backward",
        ).rename(columns={"rsi_2h_val": "rsi_2h"})
        micro_window = 64     # 64 × 1H ≈ 32 × 2H bars
    elif tf in ("4H",):
        # 4H > 2H: use rsi_base as proxy, rolling over 16 4H bars
        df["rsi_2h"]  = df["rsi_base"]
        micro_window  = 32    # 32 × 4H ≈ 128H ≈ 16 × 2H daily
    elif tf in ("15m",):
        # Resample 15m to 2H (8 bars per period)
        tmp2h = (df.set_index("datetime_utc")["close"]
                   .resample("2h").last().dropna().reset_index())
        tmp2h["rsi_2h_val"] = rsi(tmp2h["close"], RSI_LEN).values
        df = pd.merge_asof(
            df.sort_values("datetime_utc"),
            tmp2h[["datetime_utc", "rsi_2h_val"]],
            on="datetime_utc", direction="backward",
        ).rename(columns={"rsi_2h_val": "rsi_2h"})
        micro_window = 128    # 128 × 15m ≈ 32H ≈ 16 × 2H bars
    else:
        df["rsi_2h"]  = df["rsi_base"]
        micro_window  = 64

    df = df.reset_index(drop=True)

    # ── 15m and 30m RSI ───────────────────────────────────────────────────────
    if tf == "15m":
        # ON 15m chart: rsi_15m = rsi_base; resample to 30m
        df["rsi_15m"] = df["rsi_base"]
        tmp30 = (df.set_index("datetime_utc")["close"]
                    .resample("30min").last().dropna().reset_index())
        tmp30["rsi_30m_val"] = rsi(tmp30["close"], RSI_LEN).values
        r30m = pd.merge_asof(
            df[["datetime_utc"]].copy(),
            tmp30[["datetime_utc", "rsi_30m_val"]],
            on="datetime_utc", direction="backward")
        df["rsi_30m"] = r30m["rsi_30m_val"].values

        # 1H RSI from 1H parquet
        r1h = merge_tf("1H", {"rsi_1h": ("close", lambda s: rsi(s, RSI_LEN))})
        df["rsi_1h"] = r1h.get("rsi_1h", df["rsi_base"].values)
    elif tf in ("1H", "4H"):
        p15 = PQ_DIR / f"H2_raw_{instrument}_15m.parquet"
        if p15.exists():
            src15 = pd.read_parquet(p15).sort_values("datetime_utc").reset_index(drop=True)
            src15["rsi_15m"] = rsi(src15["close"], RSI_LEN)
            tmp30 = (src15.set_index("datetime_utc")["close"]
                          .resample("30min").last().dropna().reset_index())
            tmp30["rsi_30m"] = rsi(tmp30["close"], RSI_LEN).values
            r15 = pd.merge_asof(df[["datetime_utc"]].copy(),
                                 src15[["datetime_utc", "rsi_15m"]],
                                 on="datetime_utc", direction="backward")
            r30 = pd.merge_asof(df[["datetime_utc"]].copy(),
                                 tmp30[["datetime_utc", "rsi_30m"]],
                                 on="datetime_utc", direction="backward")
            df["rsi_15m"] = r15["rsi_15m"].values
            df["rsi_30m"] = r30["rsi_30m"].values
        else:
            df["rsi_15m"] = np.nan
            df["rsi_30m"] = np.nan
        df["rsi_1h"] = df["rsi_base"] if tf == "1H" else df.get("rsi_4h", df["rsi_base"])
    else:
        df["rsi_15m"] = np.nan
        df["rsi_30m"] = np.nan
        df["rsi_1h"]  = df["rsi_base"]

    # ── Flow score (5 TFs) ────────────────────────────────────────────────────
    def slope_sign(col: str, lb: int = 4) -> pd.Series:
        if col not in df.columns or df[col].isna().all():
            return pd.Series(0.0, index=df.index)
        return np.sign(df[col] - df[col].shift(lb)).fillna(0)

    df["flow_score"] = (
        slope_sign("rsi_15m") +
        slope_sign("rsi_30m") +
        slope_sign("rsi_base") +
        slope_sign("rsi_4h") +
        slope_sign("rsi_1d")
    ).astype(float)

    # ── Micro completion ──────────────────────────────────────────────────────
    df["rsi_2h_peak"] = df["rsi_2h"].rolling(micro_window, min_periods=10).max()
    df["micro"]       = ((df["rsi_2h_peak"] - df["rsi_2h"]) / 60.0 * 100.0).clip(0, 100)

    # ── Mini composite ────────────────────────────────────────────────────────
    r15f = df["rsi_15m"].fillna(df["rsi_base"])
    r30f = df["rsi_30m"].fillna(df["rsi_base"])
    df["mini"] = (r15f + r30f + df["rsi_base"]) / 3.0

    # ── Pivot detection: per-instrument calibrated params ────────────────────
    left, right, min_age, min_swing_atr = f_get_fractal_params(instrument, tf)

    ph_arr = pivot_highs(df["high"].values, left, right)
    pl_arr = pivot_lows( df["low"].values,  left, right)

    lph      = last_confirmed_pivot(ph_arr, right)      # last confirmed pivot high
    lpl      = last_confirmed_pivot(pl_arr, right)      # last confirmed pivot low
    lph_idx  = last_confirmed_pivot_idx(ph_arr, right)  # bar index of that pivot
    lpl_idx  = last_confirmed_pivot_idx(pl_arr, right)
    lph_prev = second_confirmed_pivot(ph_arr, right)    # prior pivot high (sweep detect)
    lpl_prev = second_confirmed_pivot(pl_arr, right)

    df["last_pivot_high"]      = lph
    df["last_pivot_low"]       = lpl
    df["last_pivot_high_idx"]  = lph_idx
    df["last_pivot_low_idx"]   = lpl_idx
    df["prior_pivot_high"]     = lph_prev
    df["prior_pivot_low"]      = lpl_prev

    # Store calibration params for use in state machine
    df.attrs["fractal_left"]      = left
    df.attrs["fractal_right"]     = right
    df.attrs["fractal_min_age"]   = min_age
    df.attrs["fractal_min_swing"] = min_swing_atr

    return df


# ══════════════════════════════════════════════════════════════════════════════
# 5. STATE MACHINE
# ══════════════════════════════════════════════════════════════════════════════

def run_state_machine(df: pd.DataFrame, instrument: str, tf: str) -> list[dict]:
    """
    Bar-by-bar 4-stage state machine using structure_engine calibration.

    Stage 0: Looking for macro direction (RSI_1D + HTF)
    Stage 1: Macro confirmed — watching for valid BOS
    Stage 2: BOS confirmed — waiting for pullback to start
    Stage 3: Pullback maturing — checking for end score
    Stage 4: Entry fired — reset immediately

    All decisions use only df.iloc[0..i].  No lookahead.
    """
    # ── Get per-instrument rules ───────────────────────────────────────────────
    rules     = f_get_entry_rules(instrument)
    sl_mult   = rules["sl_atr_mult"]
    tp_a      = rules["tp_type_a"]
    tp_b      = rules["tp_type_b"]

    min_swing_atr = df.attrs.get("fractal_min_swing", 0.8)

    n = len(df)

    # Extract numpy arrays for speed
    closes   = df["close"].values
    opens    = df["open"].values
    highs    = df["high"].values
    lows     = df["low"].values
    atrs     = df["atr14"].values
    rsi1d    = df["rsi_1d"].values
    flow     = df["flow_score"].values
    htf      = df["htf_align"].values
    micro    = df["micro"].values
    mini     = df["mini"].values
    rsi_2h   = df["rsi_2h"].values
    rsi_30m  = df["rsi_30m"].values
    rsi_1h   = df["rsi_1h"].values
    rsi_4h   = df["rsi_4h"].values
    rsi_1d_  = df["rsi_1d"].values
    ema50_4h = df["ema50_4h"].values
    lph      = df["last_pivot_high"].values
    lpl      = df["last_pivot_low"].values
    lph_idx  = df["last_pivot_high_idx"].values
    lpl_idx  = df["last_pivot_low_idx"].values
    pp_hi    = df["prior_pivot_high"].values
    pp_lo    = df["prior_pivot_low"].values
    sessions = df["session"].values

    signals  = []

    stage      = 0
    macro_dir  = 0
    bos_grade  = ""
    bos_type   = ""
    bos_level  = np.nan
    bos_bar    = 0
    stage_bar  = 0
    swing_orig = np.nan    # swing low (bull) or swing high (bear) before BOS

    # Track last used BOS level to prevent re-triggering on same fractal
    last_bos_ph = np.nan
    last_bos_pl = np.nan

    # Flow history for Condition D (was negative N bars ago, now >= 0)
    flow_hist = np.full(6, np.nan)

    for i in range(30, n):    # skip warmup

        # ── Safe scalar extraction ────────────────────────────────────────────
        def _f(arr, default=0.0):
            v = arr[i]
            return float(v) if not np.isnan(v) else default

        fi   = _f(flow)
        r1   = _f(rsi1d, 50.0)
        h    = _f(htf,   0.0)
        mic  = _f(micro, 0.0)
        mn   = _f(mini,  50.0)
        r30  = _f(rsi_30m, mn)     # fallback to mini if 30m not available
        r2h  = _f(rsi_2h, 50.0)
        r1h_ = _f(rsi_1h, 50.0)
        e50  = _f(ema50_4h, closes[i])
        cl   = closes[i]
        sess = sessions[i]

        # Update flow history (index 0 = most recent)
        flow_hist = np.roll(flow_hist, 1)
        flow_hist[0] = fi

        # ── STAGE 0: looking for macro direction ──────────────────────────────
        if stage == 0:
            macro_bull = r1 > 55 and h >= 1
            macro_bear = r1 < 45 and h <= -1
            if macro_bull:
                stage = 1; macro_dir = 1;  stage_bar = i
                swing_orig = float(lpl[i]) if not np.isnan(lpl[i]) else np.nan
            elif macro_bear:
                stage = 1; macro_dir = -1; stage_bar = i
                swing_orig = float(lph[i]) if not np.isnan(lph[i]) else np.nan

        # ── STAGE 1: macro confirmed, watching for BOS ────────────────────────
        elif stage == 1:
            # Hard reset: RSI_1D crosses decisively against macro direction
            if macro_dir == 1  and r1 < 42:
                stage = 0; macro_dir = 0; continue
            if macro_dir == -1 and r1 > 58:
                stage = 0; macro_dir = 0; continue

            # Update swing origin as most recent pivot in opposite direction
            if macro_dir == 1 and not np.isnan(lpl[i]):
                swing_orig = float(lpl[i])   # track most recent swing low
            elif macro_dir == -1 and not np.isnan(lph[i]):
                swing_orig = float(lph[i])   # track most recent swing high

            # Check for BOS in macro direction
            _bos_found = False
            if macro_dir == 1 and not np.isnan(lph[i]):
                ph_val = float(lph[i])
                ph_bar = int(lph_idx[i]) if not np.isnan(lph_idx[i]) else 0
                if (ph_val != last_bos_ph and
                        cl > ph_val and
                        i > 0 and closes[i - 1] <= ph_val):
                    _bos_found = True
                    bos_dir    = 1
                    bos_val    = ph_val
                    bos_bidx   = ph_bar
                    _pp        = float(pp_hi[i]) if not np.isnan(pp_hi[i]) else None

            elif macro_dir == -1 and not np.isnan(lpl[i]):
                pl_val = float(lpl[i])
                pl_bar = int(lpl_idx[i]) if not np.isnan(lpl_idx[i]) else 0
                if (pl_val != last_bos_pl and
                        cl < pl_val and
                        i > 0 and closes[i - 1] >= pl_val):
                    _bos_found = True
                    bos_dir    = -1
                    bos_val    = pl_val
                    bos_bidx   = pl_bar
                    _pp        = float(pp_lo[i]) if not np.isnan(pp_lo[i]) else None

            if _bos_found:
                # ── Session validity gate ─────────────────────────────────────
                if not f_check_session_valid(instrument, sess):
                    pass   # invalid session: skip this BOS, keep watching
                else:
                    # ── Minimum BOS score for this instrument + session ────────
                    min_bos = f_get_min_bos_for_session(instrument, sess)
                    atr_v   = _f(atrs, 1.0) if _f(atrs) > 0 else 1.0

                    bos_bar_data = {
                        "direction":       bos_dir,
                        "close":           cl,
                        "open":            opens[i],
                        "high":            highs[i],
                        "low":             lows[i],
                        "close_prev":      closes[i - 1],
                        "fractal_level":   bos_val,
                        "fractal_bar_idx": bos_bidx,
                        "current_bar_idx": i,
                        "swing_origin":    swing_orig if not np.isnan(swing_orig) else bos_val,
                        "atr14":           atr_v,
                        "rsi_1h":          r1h_,
                        "rsi_1d":          r1,
                        "micro":           mic,
                        "mini":            mn,
                        "flow_score":      fi,
                        "ema50_4h":        e50,
                        "prior_pivot_high": _pp if bos_dir == 1 else None,
                        "prior_pivot_low":  _pp if bos_dir == -1 else None,
                        "session":          sess,
                    }
                    bos_result = f_classify_bos(instrument, tf, bos_bar_data)

                    if (bos_result["valid"] and
                            bos_result["importance"] >= min_bos and
                            bos_result["grade"] not in ("SKIP",)):

                        stage      = 2
                        bos_grade  = bos_result["grade"]
                        bos_type   = bos_result["bos_type"]
                        bos_level  = bos_val
                        bos_bar    = i
                        stage_bar  = i

                        if bos_dir == 1:
                            last_bos_ph = bos_val
                        else:
                            last_bos_pl = bos_val

        # ── STAGE 2: BOS confirmed, waiting for pullback ──────────────────────
        elif stage == 2:
            if i - stage_bar > MAX_STAGE_TIME:
                stage = 0; macro_dir = 0; continue

            # Rule 5: if next bar immediately closes back through BOS level, invalidate
            if i == bos_bar + 1:
                if macro_dir == 1  and cl < bos_level:
                    stage = 1; continue     # BOS failed — return to watching
                if macro_dir == -1 and cl > bos_level:
                    stage = 1; continue

            # Pullback starts when micro building or price retraces toward bos_level
            pb_bull = (macro_dir == 1  and
                       (mic > 20 or cl < bos_level * 1.002))
            pb_bear = (macro_dir == -1 and
                       (mic > 20 or cl > bos_level * 0.998))
            if pb_bull or pb_bear:
                stage = 3; stage_bar = i

        # ── STAGE 3: pullback maturing ────────────────────────────────────────
        elif stage == 3:
            if i - stage_bar > MAX_STAGE_TIME:
                stage = 0; macro_dir = 0; continue

            # Zone 4 invalidation: pullback too deep — BOS is likely failing.
            # GUARD: only apply Fibonacci zone check when swing span >= 3 × ATR.
            # A swing smaller than 3 ATR is a local micro-pivot, not a meaningful
            # structural swing — its Fibonacci levels are noise and would trigger
            # false Zone 4 resets on every normal SL-sized pullback.
            atr_v_s3  = _f(atrs, 1.0)
            full_range = abs(bos_level - float(swing_orig)) if not np.isnan(swing_orig) else 0.0
            if (not np.isnan(swing_orig) and bos_level != swing_orig and
                    full_range >= 3.0 * atr_v_s3):
                pz = f_get_pullback_zone(
                    current_price = cl,
                    bos_level     = bos_level,
                    swing_origin  = float(swing_orig),
                    direction     = macro_dir,
                    instrument    = instrument,
                )
                if pz["zone"] == 4:
                    stage = 0; macro_dir = 0; continue  # BOS failed — retrace too deep
                pb_zone = pz["zone"]
            else:
                pb_zone = 2   # swing too small for Fibonacci: assume standard zone

            # Build bar dict for end score evaluation
            mn2   = float(mini[i - 2]) if i >= 2 and not np.isnan(mini[i - 2]) else mn
            # FIX: r30_1 fallback must use PREVIOUS bar's mini, not current r30.
            # If both current and prev 30m RSI are NaN, we fall back to mini slope.
            mn1   = float(mini[i - 1]) if i >= 1 and not np.isnan(mini[i - 1]) else mn
            r30_1 = float(rsi_30m[i - 1]) if i >= 1 and not np.isnan(rsi_30m[i - 1]) else mn1
            r2h_1 = float(rsi_2h[i - 1]) if i >= 1 and not np.isnan(rsi_2h[i - 1]) else r2h
            fl3   = float(flow_hist[3]) if not np.isnan(flow_hist[3]) else 0.0

            end_bar = {
                "mini":        mn,
                "mini_prev":   float(mini[i - 1]) if i >= 1 and not np.isnan(mini[i-1]) else mn,
                "mini_prev2":  mn2,
                "rsi_30m":     r30,
                "rsi_30m_prev": r30_1,
                "micro":       mic,
                "flow":        fi,
                "flow_3bars":  fl3,
                "rsi_2h":      r2h,
                "rsi_2h_prev": r2h_1,
            }
            end_result = f_check_pullback_end(end_bar, instrument, tf,
                                               macro_dir, pb_zone)

            if end_result["ready"]:
                stage = 4
                signals.append({
                    "bar":           i,
                    "direction":     macro_dir,
                    "bos_type":      bos_type,
                    "bos_grade":     bos_grade,
                    "bos_level":     bos_level,
                    "swing_origin":  float(swing_orig) if not np.isnan(swing_orig) else bos_level,
                    "pb_zone":       pb_zone,
                    "end_score":     end_result["end_score"],
                    "end_conds":     end_result["conditions"],
                    "micro":         mic,
                    "mini":          mn,
                    "flow":          fi,
                    "rsi_1d":        r1,
                    "session":       sess,
                    "date":          str(df["datetime_utc"].iloc[i]),
                    # per-instrument trade params (for simulate_trades)
                    "sl_mult":       sl_mult,
                    "tp_a":          tp_a,
                    "tp_b":          tp_b,
                })

        # ── STAGE 4: entry fired, reset ───────────────────────────────────────
        elif stage == 4:
            stage = 0; macro_dir = 0
            last_bos_ph = np.nan; last_bos_pl = np.nan

    return signals


# ══════════════════════════════════════════════════════════════════════════════
# 5B. STATE MACHINE v4 — 6-LAYER STRUCTURAL ENTRY GATE
# ══════════════════════════════════════════════════════════════════════════════

def run_state_machine_v4(df: pd.DataFrame, instrument: str, tf: str,
                          min_entry_score: int = 50,
                          min_pullback_total: int = 9,
                          min_rr: float = 2.0) -> list[dict]:
    """
    v4 signal generator: replaces the 4-stage macro/BOS/pullback/end-score
    pipeline with the 6-layer structural entry gate.

    Gate order (cheap → expensive):
      G1: Macro state ∈ {PULLBACK, REACCUMULATION, LIQ_SWEEP}
      G2: Location  ∈ {LATE_PULLBACK, PULLBACK_COMPLETE, REACCUM_CONFIRMED,
                        SWEEP_AT_LOW, SWEEP_AT_HIGH}
      G3: Session valid for instrument
      G4: Anti-spam (≥5 bars since last entry, same BOS level not reused)
      G5–G7: entry_score.f_score_entry() → rr >= 2.0, pb >= 9, total >= 50

    All decisions use only df.iloc[0..i].  No lookahead bias.
    Macro state machine is pre-computed (it is itself walk-forward).
    """
    # ── Lazy imports inside function to avoid stdout double-wrap ──────────
    from macro_structure_engine import run_macro_state_machine
    from entry_score            import f_score_entry

    rules   = f_get_entry_rules(instrument)
    sl_mult = rules["sl_atr_mult"]
    tp_a    = rules["tp_type_a"]
    tp_b    = rules["tp_type_b"]

    # ── Pre-compute macro states (walk-forward, no lookahead) ─────────────
    macro_states  = run_macro_state_machine(df, instrument, tf)
    macro_by_bar  = {s["bar"]: s for s in macro_states}

    VALID_STATES = {"PULLBACK", "REACCUMULATION", "LIQ_SWEEP"}
    VALID_LOCS   = {
        "LATE_PULLBACK", "PULLBACK_COMPLETE", "REACCUM_CONFIRMED",
        "SWEEP_AT_LOW",  "SWEEP_AT_HIGH",
    }

    sessions        = df["session"].values
    n               = len(df)
    signals: list[dict] = []
    last_entry_bar  = -20
    last_bos_used   = np.nan
    recent_pb_totals: list[int] = []   # for momentum building context

    for i in range(50, n):

        # ── G1: Structural state ──────────────────────────────────────────
        ms = macro_by_bar.get(i)
        if ms is None:
            recent_pb_totals = []
            continue

        state     = ms["state"]
        if state not in VALID_STATES:
            recent_pb_totals = []
            continue

        location  = ms["location"]
        macro_dir = ms.get("macro_dir", 0)
        bos_level = ms.get("bos_level") or float(df["close"].iloc[i])

        # ── G2: Location quality ──────────────────────────────────────────
        if location not in VALID_LOCS:
            continue

        # ── G3: Session valid ─────────────────────────────────────────────
        sess = sessions[i]
        if not f_check_session_valid(instrument, sess):
            continue

        # ── G4: Anti-spam ─────────────────────────────────────────────────
        if i - last_entry_bar < 5:
            continue
        atr_i = max(float(df["atr14"].iloc[i]), 1e-6)
        if (not np.isnan(last_bos_used) and
                abs(bos_level - last_bos_used) < 0.5 * atr_i):
            continue

        # ── G5–G7: 6-layer entry score (single call, extracts all gates) ──
        try:
            es = f_score_entry(
                df              = df,
                bar_idx         = i,
                direction       = macro_dir,
                bos_level       = bos_level,
                state           = state,
                location        = location,
                instrument      = instrument,
                markov_prob     = 0.0,      # Markov engine not yet wired
                prior_pb_totals = (recent_pb_totals[-4:] if recent_pb_totals else None),
            )
        except Exception:
            continue

        # Track pullback total for momentum building even on rejects
        recent_pb_totals.append(es.pullback_total)
        if len(recent_pb_totals) > 6:
            recent_pb_totals.pop(0)

        # RR gate (embedded in L3)
        if es.rr_available < min_rr:
            continue

        # Pullback gate
        if es.pullback_total < min_pullback_total:
            continue

        # Entry score gate
        if es.total < min_entry_score:
            continue

        # ── All gates passed → fire signal ────────────────────────────────
        last_entry_bar = i
        last_bos_used  = bos_level

        lm = es.liquidity_map

        def _safe(col: str, default: float = 0.0) -> float:
            try:
                v = df[col].iloc[i]
                return float(v) if not np.isnan(float(v)) else default
            except Exception:
                return default

        signals.append({
            "bar":          i,
            "direction":    macro_dir,
            "bos_type":     "A",          # v4: all structural entries = type A
            "bos_grade":    es.grade,
            "bos_level":    bos_level,
            "swing_origin": bos_level,
            "pb_zone":      2,
            "end_score":    es.pullback_total,
            "end_conds":    {},
            "micro":        _safe("micro"),
            "mini":         _safe("mini", 50.0),
            "flow":         _safe("flow_score"),
            "rsi_1d":       _safe("rsi_1d", 50.0),
            "session":      sess,
            "date":         str(df["datetime_utc"].iloc[i]),
            "sl_mult":      sl_mult,
            "tp_a":         tp_a,
            "tp_b":         tp_b,
            # v4 extras for analysis
            "state":        state,
            "location":     location,
            "entry_score":  es.total,
            "entry_grade":  es.grade,
            "pb_score":     es.pullback_total,
            "rr_available": es.rr_available,
            "l1":           es.l1_structure,
            "l2":           es.l2_location,
            "l3":           es.l3_liquidity,
            "l5":           es.l5_pullback,
            "momentum_bld": es.momentum.building,
        })

    return signals


# ══════════════════════════════════════════════════════════════════════════════
# 6. TRADE SIMULATION
# ══════════════════════════════════════════════════════════════════════════════

def simulate_trades(signals: list[dict], df: pd.DataFrame,
                    split_bar: int) -> list[dict]:
    """
    Simulate each signal bar-by-bar with per-signal sl/tp params.
    Conservative: within a bar, SL is checked before TP.
    No pyramiding.
    """
    n      = len(df)
    closes = df["close"].values
    highs  = df["high"].values
    lows   = df["low"].values
    atrs   = df["atr14"].values

    trades   = []
    in_trade = False

    for sig in signals:
        ebar = sig["bar"]
        if in_trade:
            continue

        direction = sig["direction"]
        ep        = closes[ebar]
        atr_v     = float(atrs[ebar])
        if atr_v <= 0 or np.isnan(atr_v):
            continue

        sl_mult = sig.get("sl_mult", 1.5)
        tp_a    = sig.get("tp_a",    3.0)
        tp_b    = sig.get("tp_b",    2.5)
        tp2_r   = tp_a if sig["bos_type"] == "A" else tp_b

        risk   = sl_mult * atr_v
        sl     = ep - direction * risk
        tp1    = ep + direction * TP1_R * risk
        tp2    = ep + direction * tp2_r * risk

        in_trade  = True
        tp1_hit   = False
        sl_curr   = sl
        exit_r    = None

        for j in range(ebar + 1, min(ebar + MAX_HOLD + 1, n)):
            bh = highs[j]; bl = lows[j]
            if direction == 1:
                if bl <= sl_curr:
                    exit_r = -1.0 if not tp1_hit else 0.0; break
                if not tp1_hit and bh >= tp1:
                    tp1_hit  = True; sl_curr = ep
                    if bh >= tp2:
                        exit_r = 0.5 * TP1_R + 0.5 * tp2_r; break
                elif tp1_hit and bh >= tp2:
                    exit_r = 0.5 * TP1_R + 0.5 * tp2_r; break
            else:
                if bh >= sl_curr:
                    exit_r = -1.0 if not tp1_hit else 0.0; break
                if not tp1_hit and bl <= tp1:
                    tp1_hit  = True; sl_curr = ep
                    if bl <= tp2:
                        exit_r = 0.5 * TP1_R + 0.5 * tp2_r; break
                elif tp1_hit and bl <= tp2:
                    exit_r = 0.5 * TP1_R + 0.5 * tp2_r; break

        if exit_r is None:
            j    = min(ebar + MAX_HOLD, n - 1)
            pm   = (closes[j] - ep) * direction
            ur   = pm / risk
            exit_r = (0.5 * TP1_R + 0.5 * ur) if tp1_hit else ur

        in_trade = False
        trades.append({
            **sig,
            "entry_price": round(ep, 5),
            "sl_price":    round(sl, 5),
            "tp1_price":   round(tp1, 5),
            "tp2_price":   round(tp2, 5),
            "exit_r":      round(float(exit_r), 4),
            "tp1_hit":     tp1_hit,
            "split":       "IS" if ebar < split_bar else "OOS",
        })

    return trades


# ══════════════════════════════════════════════════════════════════════════════
# 7. METRICS
# ══════════════════════════════════════════════════════════════════════════════

def calc_metrics(trades: list[dict]) -> dict:
    if not trades:
        return dict(n=0, wr=0, ev=0, sharpe=0, maxdd=0, pf=0)
    rs  = np.array([t["exit_r"] for t in trades])
    eq  = np.cumsum(rs); pk = np.maximum.accumulate(eq)
    dd  = float((eq - pk).min())
    w   = rs[rs > 0]; l = rs[rs < 0]
    pf  = float(w.sum() / abs(l.sum())) if l.sum() != 0 else float("inf")
    bpy = 6240   # 1H bars per year (24/5 market; roughly correct for forex)
    sh  = (float(rs.mean() / rs.std() * np.sqrt(bpy / len(rs)))
           if rs.std() > 0 else 0.0)
    return dict(n=len(rs), wr=round(float((rs > 0).mean() * 100), 1),
                ev=round(float(rs.mean()), 4), sharpe=round(sh, 3),
                maxdd=round(dd, 3), pf=round(pf, 3))


def mc_ruin(trades: list[dict], n_paths: int = 1000) -> float:
    if len(trades) < 5:
        return 1.0
    rs  = np.array([t["exit_r"] for t in trades])
    rng = np.random.default_rng(42)
    bad = sum(
        1 for _ in range(n_paths)
        if np.any(100.0 + np.cumsum(rng.choice(rs, size=len(rs), replace=True)) < 20.0)
    )
    return bad / n_paths


def gh_score(sharpe: float, maxdd: float, ruin: float) -> float:
    if maxdd == 0 or sharpe <= 0:
        return 0.0
    return round(sharpe * (1.0 / abs(maxdd)) * (1.0 - ruin), 4)


def tier(sharpe: float, ruin: float, n: int) -> str:
    if sharpe >= 2.0 and ruin < 0.02 and n >= 15: return "Tier 1"
    if sharpe >= 1.0 and ruin < 0.05 and n >= 10: return "Tier 2"
    if sharpe >= 0.0 and ruin < 0.10:             return "Tier 3"
    return "Avoid"


# ══════════════════════════════════════════════════════════════════════════════
# 8. PER-INSTRUMENT RUNNER
# ══════════════════════════════════════════════════════════════════════════════

def run_instrument(instrument: str, tf: str = "1H",
                   flag: str | None = None,
                   version: str = "v2",
                   min_entry_score: int = 50,
                   min_pullback_total: int = 9,
                   min_rr: float = 2.0) -> dict | None:
    """
    Run backtest for one instrument/timeframe.
    version: "v2"       = original 4-stage BOS engine
             "v4"       = 6-layer structural entry gate, strict (score≥50, pb≥9)
             "v4_loose" = same but score≥40, pb≥7
    """
    df = build_features(instrument, tf)
    if df is None or len(df) < 200:
        return None

    n_bars    = len(df)
    split_bar = int(n_bars * (1.0 - OOS_FRAC))

    if version in ("v4", "v4_loose"):
        signals = run_state_machine_v4(df, instrument, tf,
                                       min_entry_score=min_entry_score,
                                       min_pullback_total=min_pullback_total,
                                       min_rr=min_rr)
    else:
        signals = run_state_machine(df, instrument, tf)
    if not signals:
        return None

    trades  = simulate_trades(signals, df, split_bar)
    oos     = [t for t in trades if t["split"] == "OOS"]
    is_tr   = [t for t in trades if t["split"] == "IS"]

    # ── OOS fallback: use IS-only when OOS period produced no trades ─────────
    # This happens when the OOS window coincides with a macro regime break
    # (e.g., JP225 Apr-Jun 2026 tariff crash: RSI_1D < 55 → macro bull blocked).
    # Report IS metrics flagged as IS-ONLY so the instrument still appears in
    # the ranking table rather than silently disappearing.
    oos_only = True
    if not oos:
        if not is_tr:
            return None        # no trades at all — skip
        oos_only = False       # signal to downstream: these metrics are IS-only
        oos    = is_tr         # evaluate on IS set, clearly flagged below
        is_tr  = []

    m_oos  = calc_metrics(oos)
    m_is   = calc_metrics(is_tr)
    ruin   = mc_ruin(oos)
    ghs    = gh_score(m_oos["sharpe"], m_oos["maxdd"], ruin)

    # BOS breakdown
    type_a = sum(1 for t in oos if t.get("bos_type") == "A")
    type_b = sum(1 for t in oos if t.get("bos_type") == "B")
    grade_ap = sum(1 for t in oos if t.get("bos_grade") == "A+")

    # Session breakdown (OOS)
    sess_counts = {}
    for t in oos:
        s = t.get("session", "?")
        sess_counts[s] = sess_counts.get(s, 0) + 1
    top_session = max(sess_counts, key=sess_counts.get) if sess_counts else "?"

    # Approximate OOS trading months
    oos_bars      = n_bars - split_bar
    h_per_bar     = {"1H": 1, "4H": 4, "15m": 0.25}.get(tf, 1)
    is_index      = instrument in ("US30", "USTEC", "DE40", "UK100",
                                   "JP225", "HK50", "SPX500", "AUS200")
    h_per_day     = 16 if is_index else 24
    trading_months = round(oos_bars * h_per_bar / h_per_day / 21, 1)
    trades_pm      = round(m_oos["n"] / max(trading_months, 0.5), 1)

    # Flag label: combine user flag with IS-only note when applicable
    flag_parts = []
    if flag:
        flag_parts.append(flag)
    if not oos_only:
        flag_parts.append("IS-ONLY — OOS window had no macro-bull conditions")
    combined_flag = " | ".join(flag_parts)

    return {
        "instrument":    instrument,
        "tf":            tf,
        "flag":          combined_flag,
        "oos_only":      oos_only,          # True = real OOS; False = IS shown as proxy
        "n_oos":         m_oos["n"],
        "n_is":          m_is["n"],
        "wr":            m_oos["wr"],
        "ev":            m_oos["ev"],
        "sharpe":        m_oos["sharpe"],
        "maxdd":         m_oos["maxdd"],
        "pf":            m_oos["pf"],
        "ruin":          round(ruin, 4),
        "gh_score":      ghs,
        "trades_pm":     trades_pm,
        "type_a":        type_a,
        "type_b":        type_b,
        "grade_ap":      grade_ap,
        "top_session":   top_session,
        "is_wr":         m_is["wr"],
        "oos_months":    trading_months,
        "tier":          tier(m_oos["sharpe"], ruin, m_oos["n"]),
        "low_sample":    m_oos["n"] < MIN_OOS_TRADES,
    }


# ══════════════════════════════════════════════════════════════════════════════
# 9. MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main(run_version: str = "v4"):
    """
    run_version: "v2"  = original engine only
                 "v4"  = v4 6-layer engine only
                 "both"= run both and print four-column comparison
    """
    ver_label = {"v2": "v2 (7-pillar BOS)", "v4": "v4 (6-layer strict)",
                 "v4_loose": "v4-loose (score>=40, pb>=7)",
                 "both": "v2 + v4 comparison",
                 "gates": "v4-strict vs v4-loose gate comparison",
                 }.get(run_version, run_version)
    print("=" * 92)
    print(f"  GOLDEN HIGHWAY ENGINE -- {ver_label}")
    print("=" * 92)

    if run_version == "both":
        versions = ["v2", "v4"]
    elif run_version == "gates":
        versions = ["v4", "v4_loose"]
    else:
        versions = [run_version]
    all_res   = {v: [] for v in versions}

    # Gate parameters per version
    _gate_params = {
        "v4":       dict(min_entry_score=50, min_pullback_total=9,  min_rr=2.0),
        "v4_loose": dict(min_entry_score=40, min_pullback_total=7,  min_rr=2.0),
        "v2":       dict(min_entry_score=50, min_pullback_total=9,  min_rr=2.0),
    }

    for ver in versions:
        gp = _gate_params.get(ver, {})
        print(f"\n  --- Running {ver} ---")
        for inst, tf, flag in INSTRUMENT_TFS:
            label = f"{inst:<8} {tf}"
            print(f"  {ver} {label}...", end="  ", flush=True)
            try:
                r = run_instrument(inst, tf, flag, version=ver, **gp)
                if r:
                    r["version"] = ver
                    is_note = " [IS-ONLY]" if not r["oos_only"] else ""
                    low     = " *" if r["low_sample"] else ""
                    print(f"OOS={r['n_oos']:>3}  Sharpe={r['sharpe']:>7.3f}"
                          f"  GH={r['gh_score']:>7.4f}  {r['tier']}{low}{is_note}")
                    all_res[ver].append(r)
                else:
                    print("no data / no signals")
            except Exception as e:
                print(f"ERROR: {e}")
                import traceback; traceback.print_exc()

    # Use primary version for the ranking table
    primary_ver = versions[-1]
    results     = all_res[primary_ver]

    if not results:
        print("  No results.")
        return

    df_r = (pd.DataFrame(results)
              .sort_values(["tier", "gh_score"], ascending=[True, False])
              .reset_index(drop=True))

    # ── Console table ──────────────────────────────────────────────────────────
    print()
    print("=" * 108)
    print("  RANKING TABLE -- ALL INSTRUMENTS (OOS)   [IS-ONLY] = no OOS trades in window")
    print("=" * 108)
    hdr = (f"  {'Inst':<8} {'TF':<4} {'Tier':<8} {'OOS':>4} {'WR%':>5} "
           f"{'EV':>7} {'Sharpe':>7} {'MaxDD':>6} {'PF':>5} "
           f"{'Ruin%':>6} {'GH':>7}  {'A+':>3} {'A':>3} {'B':>3} "
           f"{'Pm':>4} {'Sess':>7}  {'IS WR':>5}  Flag")
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))

    for _, row in df_r.iterrows():
        lf   = " *" if row["low_sample"] else "  "
        is_tag = " [IS-ONLY]" if not row.get("oos_only", True) else ""
        fl   = f" [{row['flag'][:28]}]{is_tag}" if (row["flag"] or is_tag) else ""
        rpc  = row["ruin"] * 100
        pf_s = f"{row['pf']:.3f}" if row["pf"] < 1e5 else "inf"
        ev_s = f"{row['ev']:+.4f}"
        print(f"  {row['instrument']:<8} {row['tf']:<4} {row['tier']:<8} "
              f"{row['n_oos']:>4}{lf} {row['wr']:>5.1f} "
              f"{ev_s:>7} {row['sharpe']:>7.3f} "
              f"{row['maxdd']:>6.3f} {pf_s:>5} "
              f"{rpc:>6.1f} {row['gh_score']:>7.4f}  "
              f"{row['grade_ap']:>3} {row['type_a']:>3} {row['type_b']:>3} "
              f"{row['trades_pm']:>4.1f} {row['top_session']:>7}  "
              f"{row['is_wr']:>5.1f}{fl}")

    print()
    print("  * = fewer than", MIN_OOS_TRADES, "OOS trades — treat with caution")

    t1 = df_r[df_r["tier"] == "Tier 1"]
    t2 = df_r[df_r["tier"] == "Tier 2"]
    t3 = df_r[df_r["tier"] == "Tier 3"]
    print(f"\n  Tier 1 ({len(t1)}): {', '.join(t1['instrument']+' '+t1['tf'])}")
    print(f"  Tier 2 ({len(t2)}): {', '.join(t2['instrument']+' '+t2['tf'])}")
    print(f"  Tier 3 ({len(t3)}): {', '.join(t3['instrument']+' '+t3['tf'])}")

    # ── JP225 highlight ────────────────────────────────────────────────────────
    jp = df_r[df_r["instrument"] == "JP225"]
    if len(jp):
        print("\n  ── JP225 RESULTS (Golden Highway solves the Markov problem?) ──")
        for _, r in jp.iterrows():
            verdict = "YES — GH works on JP225" if r["sharpe"] >= 1.0 else \
                      "PARTIAL" if r["sharpe"] >= 0 else "NO — structure also struggles"
            print(f"    {r['tf']:<4}  Sharpe={r['sharpe']:.3f}  GH={r['gh_score']:.4f}"
                  f"  Tier={r['tier']:<8}  OOS={r['n_oos']}  -> {verdict}")

    # ── Gate comparison (v4-strict vs v4-loose) ───────────────────────────────
    if run_version == "gates" and "v4" in all_res and "v4_loose" in all_res:
        df_strict = pd.DataFrame(all_res["v4"]).set_index(["instrument","tf"])
        df_loose  = pd.DataFrame(all_res["v4_loose"]).set_index(["instrument","tf"])
        print("\n")
        print("=" * 110)
        print("  GATE COMPARISON: v4-strict (score>=50, pb>=9) vs v4-loose (score>=40, pb>=7)")
        print("  Key question: does loosening gates lift trade count while WR stays >= 45%?")
        print("=" * 110)
        print(f"\n  {'Inst+TF':<14}  {'Strict N':>8} {'Strict WR':>9} {'Strict Sh':>9}  "
              f"{'Loose N':>7} {'Loose WR':>8} {'Loose Sh':>8}  "
              f"{'dN':>4} {'WR ok?':>6}  Verdict")
        print("  " + "-" * 108)

        all_keys = set(df_strict.index) | set(df_loose.index)
        for key in sorted(all_keys):
            inst, tf_k = key
            rs = df_strict.loc[key] if key in df_strict.index else None
            rl = df_loose.loc[key]  if key in df_loose.index  else None
            sn  = int(rs["n_oos"])    if rs is not None else 0
            sw  = float(rs["wr"])     if rs is not None else 0.0
            ssh = float(rs["sharpe"]) if rs is not None else 0.0
            ln  = int(rl["n_oos"])    if rl is not None else 0
            lw  = float(rl["wr"])     if rl is not None else 0.0
            lsh = float(rl["sharpe"]) if rl is not None else 0.0
            dn  = ln - sn
            wr_ok = "YES" if lw >= 45.0 else "NO "
            if ln >= 30 and lw >= 45.0 and lsh > 0:
                verdict = "TRADEABLE"
            elif ln >= 15 and lw >= 45.0:
                verdict = "promising"
            elif dn > 5 and lw < 45.0:
                verdict = "more noise"
            else:
                verdict = "—"
            print(f"  {inst:<8} {tf_k:<4}  {sn:>8} {sw:>8.1f}% {ssh:>9.3f}  "
                  f"{ln:>7} {lw:>7.1f}% {lsh:>8.3f}  "
                  f"{dn:>+4} {wr_ok:>6}  {verdict}")

        print("  " + "-" * 108)
        tradeable = [(k[0]+' '+k[1]) for k in sorted(all_keys)
                     if k in df_loose.index
                     and int(df_loose.loc[k]["n_oos"]) >= 30
                     and float(df_loose.loc[k]["wr"]) >= 45.0
                     and float(df_loose.loc[k]["sharpe"]) > 0]
        promising = [(k[0]+' '+k[1]) for k in sorted(all_keys)
                     if k in df_loose.index
                     and 15 <= int(df_loose.loc[k]["n_oos"]) < 30
                     and float(df_loose.loc[k]["wr"]) >= 45.0]
        print(f"\n  TRADEABLE  (N>=30, WR>=45%, Sharpe>0): {tradeable or 'none'}")
        print(f"  PROMISING  (N>=15, WR>=45%):            {promising or 'none'}")

    # ── Four-column comparison (v1 / v2 / v3 / v4) when "both" requested ─────
    if run_version == "both" and "v2" in all_res and all_res["v2"]:
        df_v2 = pd.DataFrame(all_res["v2"]).set_index(["instrument","tf"])
        df_v4 = df_r.set_index(["instrument","tf"]) if "instrument" in df_r.columns else pd.DataFrame()

        # Load v1 data from the earlier signal file if available
        v1_path = OUT_DIR / "golden_highway_backtest.csv"
        v1_oos_wr = None; v1_oos_sharpe = None
        if v1_path.exists():
            v1_raw = pd.read_csv(v1_path)
            s04    = v1_raw[v1_raw["signal"].str.contains("Phase|Journey", na=False)]
            if len(s04):
                v1_oos_wr     = float(s04.iloc[0]["oos_win_rate"])
                v1_oos_sharpe = float(s04.iloc[0]["oos_sharpe"])

        print("\n")
        print("=" * 100)
        print("  FOUR-WAY COMPARISON: v1 / v2 / v3 / v4")
        print("  v1 = Journey Phase 6   v2 = 7-pillar BOS   "
              "v3 = v2 + JP225 recal   v4 = 6-layer wired")
        print("=" * 100)
        print(f"\n  {'Inst+TF':<14}  {'v2 Tier':<8} {'v2 WR':>6} {'v2 Sh':>7}  "
              f"{'v4 Tier':<8} {'v4 WR':>6} {'v4 Sh':>7}  "
              f"{'Delta WR':>8} {'Delta Sh':>9}  Direction")
        print("  " + "-" * 98)

        all_keys = set(df_v2.index) | set(df_v4.index)
        rows_cmp = []
        for key in sorted(all_keys):
            inst, tf_k = key
            r2 = df_v2.loc[key] if key in df_v2.index else None
            r4 = df_v4.loc[key] if key in df_v4.index else None
            v2_tier = r2["tier"] if r2 is not None else "n/a"
            v4_tier = r4["tier"] if r4 is not None else "no signals"
            v2_wr   = float(r2["wr"])     if r2 is not None else 0.0
            v4_wr   = float(r4["wr"])     if r4 is not None else 0.0
            v2_sh   = float(r2["sharpe"]) if r2 is not None else 0.0
            v4_sh   = float(r4["sharpe"]) if r4 is not None else 0.0
            dwr     = v4_wr - v2_wr
            dsh     = v4_sh - v2_sh
            arrow   = ("IMPROVED" if dsh > 0.5 else
                       "REGRESSED" if dsh < -0.5 else "UNCHANGED")
            rows_cmp.append((key, v2_tier, v2_wr, v2_sh, v4_tier, v4_wr, v4_sh, dwr, dsh, arrow))
            print(f"  {inst:<8} {tf_k:<4}  {v2_tier:<8} {v2_wr:>5.1f}% {v2_sh:>7.3f}  "
                  f"{v4_tier:<8} {v4_wr:>5.1f}% {v4_sh:>7.3f}  "
                  f"{dwr:>+7.1f}pp {dsh:>+8.3f}  {arrow}")

        print("  " + "-" * 98)
        if v1_oos_wr is not None:
            print(f"\n  v1 XAUUSD baseline: WR={v1_oos_wr:.1f}%  Sharpe={v1_oos_sharpe:.3f}")

        improved  = [r for r in rows_cmp if r[-1] == "IMPROVED"]
        regressed = [r for r in rows_cmp if r[-1] == "REGRESSED"]
        print(f"\n  IMPROVED  ({len(improved)}): {', '.join(r[0][0]+' '+r[0][1] for r in improved)}")
        print(f"  REGRESSED ({len(regressed)}): {', '.join(r[0][0]+' '+r[0][1] for r in regressed)}")

        # Best v4 instrument
        v4_tier1 = [(r[0], r[6]) for r in rows_cmp if r[4] == "Tier 1"]
        v4_t2t3  = [(r[0], r[4], r[6]) for r in rows_cmp if r[4] in ("Tier 1","Tier 2","Tier 3")]
        if v4_t2t3:
            best    = max(v4_t2t3, key=lambda x: x[2])
            print(f"  BEST v4:   {best[0][0]} {best[0][1]}  ({best[1]}, Sharpe={best[2]:.3f})")

    # ── Save ───────────────────────────────────────────────────────────────────
    suffix   = f"_{primary_ver}" if primary_ver != "v2" else ""
    csv_path = OUT_DIR / f"GH_instrument_ranking{suffix}.csv"
    df_r.to_csv(csv_path, index=False)
    print(f"\n  Saved: {csv_path.name}")

    _save_html(df_r)
    print(f"  Saved: GH_instrument_ranking.html")
    print("=" * 88)

    return df_r


# ══════════════════════════════════════════════════════════════════════════════
# 10. HTML REPORT
# ══════════════════════════════════════════════════════════════════════════════

def _save_html(df_r: pd.DataFrame):
    tier_bg    = {"Tier 1": "#0d2b0d", "Tier 2": "#0d1a2b",
                  "Tier 3": "#2b2b0d", "Avoid":  "#2b0d0d"}
    tier_color = {"Tier 1": "#3fb950", "Tier 2": "#58a6ff",
                  "Tier 3": "#e3b341", "Avoid":  "#f85149"}

    rows_html = ""
    for _, r in df_r.iterrows():
        bg  = tier_bg.get(r["tier"],    "#161b22")
        bc  = tier_color.get(r["tier"], "#8b949e")
        lf  = " *" if r["low_sample"] else ""
        pf_s = f"{r['pf']:.3f}" if r["pf"] < 1e5 else "inf"
        flag_cell = (f'<br><span style="color:#e3b341;font-size:10px">'
                     f'{r["flag"][:60]}</span>') if r["flag"] else ""
        rows_html += (
            f'<tr style="background:{bg}">'
            f'<td><b>{r["instrument"]}</b> <span style="color:#484f58">{r["tf"]}</span>{flag_cell}</td>'
            f'<td><span style="color:{bc};font-weight:700">{r["tier"]}</span></td>'
            f'<td>{r["n_oos"]}{lf}</td>'
            f'<td>{r["wr"]:.1f}%</td>'
            f'<td style="color:{"#3fb950" if r["ev"]>0 else "#f85149"}">{r["ev"]:+.4f}R</td>'
            f'<td>{r["sharpe"]:.3f}</td>'
            f'<td style="color:#f85149">{r["maxdd"]:.3f}R</td>'
            f'<td>{pf_s}</td>'
            f'<td style="color:{"#f85149" if r["ruin"]>0.05 else "#3fb950"}">'
            f'{r["ruin"]*100:.1f}%</td>'
            f'<td style="color:#ffd700"><b>{r["gh_score"]:.4f}</b></td>'
            f'<td>{r["grade_ap"]}</td><td>{r["type_a"]}</td><td>{r["type_b"]}</td>'
            f'<td>{r["top_session"]}</td>'
            f'<td>{r["trades_pm"]:.1f}/mo</td>'
            f'<td>{r["is_wr"]:.1f}%</td>'
            f'</tr>\n'
        )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Golden Highway v2 -- Instrument Ranking</title>
<style>
  body{{background:#0d1117;color:#e6edf3;font-family:'Segoe UI',sans-serif;margin:0;padding:20px;font-size:13px}}
  h1{{color:#ffd700;border-bottom:1px solid #30363d;padding-bottom:12px}}
  table{{width:100%;border-collapse:collapse;margin-top:8px}}
  th{{background:#161b22;color:#ffd700;padding:8px 9px;text-align:left;border-bottom:2px solid #30363d;white-space:nowrap}}
  td{{padding:6px 9px;border-bottom:1px solid #21262d;vertical-align:top}}
  .leg{{display:flex;gap:12px;margin:10px 0;font-size:12px;flex-wrap:wrap}}
  .li{{padding:4px 10px;border-radius:4px}}
</style>
</head>
<body>
<h1>Golden Highway v2 -- Instrument Ranking (Phase 1A)</h1>
<p style="color:#8b949e">
  4-stage state machine: Macro → BOS → Pullback → Entry &nbsp;|&nbsp;
  Per-instrument fractal calibration (structure_engine.py) &nbsp;|&nbsp;
  7-pillar BOS scoring &nbsp;|&nbsp; Session validity gate &nbsp;|&nbsp;
  OOS = last 20% per instrument &nbsp;|&nbsp; MC ruin = 1000 paths
</p>
<div class="leg">
  <span class="li" style="background:#0d2b0d;color:#3fb950">Tier 1: Sharpe&ge;2, Ruin&lt;2%, &ge;15 OOS</span>
  <span class="li" style="background:#0d1a2b;color:#58a6ff">Tier 2: Sharpe&ge;1, Ruin&lt;5%, &ge;10 OOS</span>
  <span class="li" style="background:#2b2b0d;color:#e3b341">Tier 3: Sharpe&ge;0, Ruin&lt;10%</span>
  <span class="li" style="background:#2b0d0d;color:#f85149">Avoid</span>
</div>
<table>
<thead><tr>
  <th>Instrument / TF</th><th>Tier</th><th>OOS</th><th>WR%</th><th>EV</th>
  <th>Sharpe</th><th>Max DD</th><th>PF</th><th>Ruin%</th>
  <th>GH Score</th><th>A+</th><th>A</th><th>B</th>
  <th>Top Session</th><th>Trades</th><th>IS WR%</th>
</tr></thead>
<tbody>{rows_html}</tbody>
</table>
<p style="color:#484f58;font-size:11px;margin-top:24px">
  * = fewer than {MIN_OOS_TRADES} OOS trades &nbsp;|&nbsp;
  GH Score = Sharpe × (1/|MaxDD|) × (1-Ruin) &nbsp;|&nbsp;
  Generated by research/golden_highway_engine.py v2
</p>
</body></html>"""
    (OUT_DIR / "GH_instrument_ranking.html").write_text(html, encoding="utf-8")


if __name__ == "__main__":
    main()
