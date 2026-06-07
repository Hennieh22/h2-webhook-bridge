"""
H2 Quant v1 — Phase 7: Live State Monitor
Runs every 5 minutes continuously. For each instrument:
  1. Pull latest bars from MT5 / yfinance
  2. Compute 22 features
  3. Classify state
  4. Load transition matrix → top 3 next states
  5. Check 5 gates
  6. Score 4 confirmation pillars
  7. Assign conviction
  8. Update H2_live_state.json (atomic write)
  9. If gates pass + conviction A/A+ → fire webhook + WhatsApp
  10. Append to H2_signal_log.csv
"""

import csv
import json
import logging
import os
import sys
import tempfile
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parent.parent
with open(ROOT / "config.yaml") as f:
    CFG = yaml.safe_load(f)

LOG_DIR = ROOT / CFG["paths"]["logs"]
LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=getattr(logging, CFG["system"]["log_level"], logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_DIR / "monitor.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("H2.monitor")

sys.path.insert(0, str(ROOT))
from features.engineer   import compute_features
from states.classifier   import StateClassifier

OUTPUTS_DIR = ROOT / CFG["paths"]["outputs"]
OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
SAST_OFFSET = timedelta(hours=2)

LIVE_TF      = CFG["monitor"]["live_timeframe"]        # "1H"
LOOKBACK     = CFG["monitor"]["lookback_bars"]          # 300
INTERVAL     = CFG["monitor"]["interval_seconds"]       # 300
DRY_RUN      = CFG["monitor"]["dry_run"]                # True
SESS_MIN_TR  = CFG["monitor"]["session_matrix_min_transitions"]

TIER1  = CFG["instruments"]["tier1"]
TIER2  = CFG["instruments"]["tier2"]
DISC   = CFG["instruments"]["discretionary"]
ALL_TIERS = TIER1 + TIER2 + DISC

GATE_GAP    = CFG["gates"]["markov_gap_min_probability"]       # 0.61
GATE_PERS   = CFG["gates"]["markov_persistence_threshold"]      # 0.82
GATE_VOL    = CFG["gates"]["volatility_cap_multiplier"]         # 1.25
GATE_HURST  = CFG["gates"]["hurst_threshold"]                   # 0.50
SESSION_GATES = CFG.get("session_gates", {})

SIGNAL_LOG_PATH  = OUTPUTS_DIR / "H2_signal_log.csv"
LIVE_STATE_PATH  = OUTPUTS_DIR / "H2_live_state.json"

SIGNAL_LOG_COLS = [
    "timestamp_utc","sast_time","instrument","direction","state_id",
    "confidence","ev","session","pillars_confirmed","conviction",
    "gate_markov_gap","gate_persistence","gate_vol","gate_hurst",
    "gate_session","all_gates_pass","fired","whatsapp_sent","outcome_r",
]

# ---------------------------------------------------------------------------
# Data acquisition
# ---------------------------------------------------------------------------

def _mt5_tf(tf_str: str):
    import MetaTrader5 as mt5
    return {
        "1m":  mt5.TIMEFRAME_M1,  "5m":  mt5.TIMEFRAME_M5,
        "15m": mt5.TIMEFRAME_M15, "1H":  mt5.TIMEFRAME_H1,
        "4H":  mt5.TIMEFRAME_H4,  "D":   mt5.TIMEFRAME_D1,
    }.get(tf_str, mt5.TIMEFRAME_H1)


def fetch_bars(instrument: str, timeframe: str, n_bars: int = LOOKBACK) -> Optional[pd.DataFrame]:
    """Pull OHLCV bars. MT5 first, yfinance fallback."""
    symbol_map = CFG["mt5"]["symbol_map"]
    symbol = symbol_map.get(instrument, instrument)

    # ── MT5 ──────────────────────────────────────────────────────────────────
    try:
        import MetaTrader5 as mt5
        if mt5.initialize():
            rates = mt5.copy_rates_from_pos(symbol, _mt5_tf(timeframe), 0, n_bars)
            mt5.shutdown()
            if rates is not None and len(rates) > 0:
                df = pd.DataFrame(rates)
                df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
                df = df.rename(columns={
                    "time": "datetime_utc", "tick_volume": "volume",
                    "open": "open", "high": "high", "low": "low", "close": "close",
                })
                df["datetime_sast"] = df["datetime_utc"] + SAST_OFFSET
                df["source"] = "mt5"
                return df[["datetime_utc","datetime_sast","open","high","low","close","volume","source"]]
    except Exception as e:
        log.debug(f"MT5 fetch failed for {instrument}: {e}")

    # ── yfinance fallback ─────────────────────────────────────────────────────
    try:
        import yfinance as yf
        ticker_map = CFG["yfinance"]["ticker_map"]
        tf_map     = CFG["yfinance"]["timeframe_map"]
        ticker = ticker_map.get(instrument)
        yf_tf  = tf_map.get(timeframe, "1h")
        period = "60d" if timeframe in ("1m","5m","15m") else "730d"
        if not ticker:
            return None
        raw = yf.download(ticker, period=period, interval=yf_tf,
                          auto_adjust=True, progress=False)
        if raw is None or raw.empty:
            return None
        if isinstance(raw.columns, pd.MultiIndex):
            raw.columns = raw.columns.get_level_values(0)
        raw.columns = [c.lower() for c in raw.columns]
        raw.index = pd.to_datetime(raw.index, utc=True)
        raw.index.name = "datetime_utc"
        raw = raw.reset_index()
        raw["volume"] = pd.to_numeric(raw.get("volume", 0), errors="coerce").fillna(0)
        raw["source"] = "yfinance"
        raw["datetime_sast"] = raw["datetime_utc"] + SAST_OFFSET
        df = raw[["datetime_utc","datetime_sast","open","high","low","close","volume","source"]].copy()
        df = df.dropna(subset=["open","high","low","close"]).tail(n_bars).reset_index(drop=True)
        log.debug(f"yfinance: {instrument} {timeframe} — {len(df)} bars")
        return df
    except Exception as e:
        log.warning(f"yfinance fetch failed for {instrument}: {e}")
        return None


# ---------------------------------------------------------------------------
# Transition matrix loader
# ---------------------------------------------------------------------------

def load_matrix(instrument: str, timeframe: str, session: str) -> tuple[dict, dict, str]:
    """
    Load transition matrix.
    Returns (prob_matrix, raw_counts, source_label).
    Uses session-conditioned matrix if it has >= SESS_MIN_TR transitions,
    falls back to global.
    """
    path = OUTPUTS_DIR / f"H2_transition_matrix_{instrument}_{timeframe}.json"
    if not path.exists():
        return {}, {}, "missing"

    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    matrices   = data.get("matrices", {})
    raw_counts = data.get("raw_counts", {})
    sess_key   = {
        "TOKYO":    "tokyo",
        "LONDON":   "london",
        "OVERLAP":  "london",
        "NY":       "ny",
        "OFFHOURS": "global",
    }.get(session, "global")

    sess_mat = matrices.get(sess_key, {})
    sess_raw = raw_counts.get(sess_key, {})
    sess_total = sum(
        sum(v for v in row.values())
        for row in sess_raw.values()
    ) if sess_raw else 0

    if sess_mat and sess_total >= SESS_MIN_TR:
        return sess_mat, sess_raw, sess_key
    return matrices.get("global", {}), raw_counts.get("global", {}), "global"


def load_state_stats(instrument: str, timeframe: str) -> dict:
    path = OUTPUTS_DIR / f"H2_state_stats_{instrument}_{timeframe}.json"
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_dwell_times(instrument: str, timeframe: str) -> dict:
    path = OUTPUTS_DIR / "H2_dwell_times.json"
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data.get(f"{instrument}_{timeframe}", {})


# ---------------------------------------------------------------------------
# Gate checker
# ---------------------------------------------------------------------------

class GateChecker:

    def check_markov_gap(self, top_prob: float) -> dict:
        passed = top_prob >= GATE_GAP
        return {"value": round(top_prob, 4), "threshold": GATE_GAP, "pass": passed}

    def check_markov_persistence(self, state_id: str, matrix: dict) -> dict:
        self_prob = matrix.get(state_id, {}).get(state_id, 0.0)
        passed = self_prob >= GATE_PERS
        return {"value": round(self_prob, 4), "threshold": GATE_PERS, "pass": passed}

    def check_volatility_cap(self, feat_row: pd.Series) -> dict:
        atr = float(feat_row.get("d03_atr__value", 0.0))
        gk  = float(feat_row.get("d03_gk_vol__value", 0.0))
        close = float(feat_row.get("close", 1.0))

        # GK vol is a decimal return, convert to price unit if needed
        if gk > 0 and close > 0:
            gk_price = gk * close   # approximate price-unit GK vol
            reference = gk_price if gk_price > 0 else atr
        else:
            reference = atr   # fallback: reference = ATR itself (ratio=1.0)

        ratio = atr / reference if reference > 0 else 1.0
        passed = ratio <= GATE_VOL
        return {"value": round(ratio, 4), "threshold": GATE_VOL, "pass": passed}

    def check_hurst(self, feat_row: pd.Series) -> dict:
        hurst = float(feat_row.get("d14_hurst__value", 0.5))
        passed = hurst >= GATE_HURST
        return {"value": round(hurst, 4), "threshold": GATE_HURST, "pass": passed}

    def check_session(self, session: str, instrument: str) -> dict:
        allowed = SESSION_GATES.get(instrument, [])
        if not allowed:
            passed = True
        else:
            passed = session in allowed
        return {"value": session, "required": allowed or "ANY", "pass": passed}

    def check_all(
        self,
        state_id:  str,
        top_prob:  float,
        matrix:    dict,
        feat_row:  pd.Series,
        session:   str,
        instrument:str,
    ) -> tuple[dict, bool]:
        gates = {
            "markov_gap":         self.check_markov_gap(top_prob),
            "markov_persistence": self.check_markov_persistence(state_id, matrix),
            "volatility_cap":     self.check_volatility_cap(feat_row),
            "hurst":              self.check_hurst(feat_row),
            "session":            self.check_session(session, instrument),
        }
        all_pass = all(g["pass"] for g in gates.values())
        return gates, all_pass


# ---------------------------------------------------------------------------
# Pillar scorer
# ---------------------------------------------------------------------------

class PillarScorer:

    def _next_state_struct(self, next_states: list) -> str:
        if not next_states:
            return "UNKNOWN"
        parts = next_states[0]["state"].split("_")
        return parts[5] + ("_" + parts[6] if len(parts) == 8 else "") if len(parts) >= 6 else "UNKNOWN"

    def pillar_volume(self, feat_row: pd.Series, direction: str) -> tuple[bool, str]:
        rvol  = float(feat_row.get("d02_rvol__value", 1.0))
        delta = float(feat_row.get("d02_delta__value", 0.0))
        delta_positive = delta > 0
        confirmed = rvol >= 1.5 and (
            (direction == "BUY"  and delta_positive) or
            (direction == "SELL" and not delta_positive)
        )
        signal = f"RVOL {rvol:.2f}x, delta {'positive' if delta_positive else 'negative'}"
        return confirmed, signal

    def pillar_structure(self, feat_df: pd.DataFrame, next_struct: str) -> tuple[bool, str]:
        last = feat_df.iloc[-1]
        bos   = float(last.get("d08_bos__value",   0.0))
        choch = float(last.get("d08_choch__value", 0.0))
        mss   = float(last.get("d08_mss__value",   0.0))

        # CHoCH in last 5 bars
        recent = feat_df.iloc[-5:]
        recent_choch = recent["d08_choch__value"].apply(lambda x: float(x) if pd.notna(x) else 0).sum()

        rules = {
            "BOS":        (bos > 0,                   "BOS printed"),
            "CHOCH":      (choch > 0,                  "CHoCH printed"),
            "MSS":        (mss > 0,                    "MSS printed"),
            "LIQ_SWEEP":  (mss > 0,                    "MSS for liq sweep"),
            "PULLBACK":   (recent_choch == 0,          "no CHoCH last 5 bars"),
            "RANGE":      (bos == 0 and choch == 0,    "no BOS/CHoCH, range intact"),
            "TREND_CONT": (bos > 0 or choch == 0,     "structure intact"),
        }
        confirmed, signal = rules.get(next_struct, (False, "no rule"))
        return bool(confirmed), signal

    def pillar_fractals(self, feat_df: pd.DataFrame, direction: str) -> tuple[bool, str]:
        recent = feat_df.iloc[-4:]   # last 3 completed bars + current
        frac_h = recent["d17_fractal_high__confidence"].apply(
            lambda x: float(x) if pd.notna(x) else 0)
        frac_l = recent["d17_fractal_low__confidence"].apply(
            lambda x: float(x) if pd.notna(x) else 0)

        if direction == "BUY":
            confirmed = (frac_l > 0).any()
            signal    = f"Fractal low printed" if confirmed else "No fractal low recent"
        else:
            confirmed = (frac_h > 0).any()
            signal    = f"Fractal high printed" if confirmed else "No fractal high recent"
        return bool(confirmed), signal

    def pillar_mtf(self, feat_row: pd.Series) -> tuple[bool, str]:
        state_4h = str(feat_row.get("d22_state_4h__value", "UNKNOWN"))
        state_1h = str(feat_row.get("d22_state_1h__value", "UNKNOWN"))

        if state_4h == "UNKNOWN" or state_1h == "UNKNOWN":
            return False, "MTF states unavailable (run Phase 3 MTF injection)"

        trend_4h = state_4h.split("_")[0] if "_" in state_4h else "NEUTRAL"
        trend_1h = state_1h.split("_")[0] if "_" in state_1h else "NEUTRAL"

        confirmed = trend_4h == trend_1h and trend_4h != "NEUTRAL"
        signal    = f"4H={trend_4h}, 1H={trend_1h} — {'aligned' if confirmed else 'diverged'}"
        return confirmed, signal

    def score(
        self,
        feat_df:    pd.DataFrame,
        next_states:list,
        direction:  str,
    ) -> tuple[dict, int]:
        if feat_df.empty:
            return {}, 0
        feat_row   = feat_df.iloc[-1]
        next_struct = self._next_state_struct(next_states)

        v_ok, v_sig = self.pillar_volume(feat_row, direction)
        s_ok, s_sig = self.pillar_structure(feat_df, next_struct)
        f_ok, f_sig = self.pillar_fractals(feat_df, direction)
        m_ok, m_sig = self.pillar_mtf(feat_row)

        pillars = {
            "volume":    {"signal": v_sig, "confirmed": v_ok},
            "structure": {"signal": s_sig, "confirmed": s_ok},
            "fractals":  {"signal": f_sig, "confirmed": f_ok},
            "mtf":       {"signal": m_sig, "confirmed": m_ok},
        }
        count = sum(1 for p in pillars.values() if p["confirmed"])
        return pillars, count


# ---------------------------------------------------------------------------
# Signal direction from state
# ---------------------------------------------------------------------------

def direction_from_state(state_id: str, next_states: list) -> str:
    """BUY for bull-biased next state, SELL for bear."""
    # prefer next state direction, fallback to current
    if next_states:
        top_parts = next_states[0]["state"].split("_")
        if top_parts[0] == "BULL":
            return "BUY"
        if top_parts[0] == "BEAR":
            return "SELL"
    parts = state_id.split("_")
    if parts[0] == "BULL":
        return "BUY"
    if parts[0] == "BEAR":
        return "SELL"
    return "BUY"   # default: bias long if neutral


# ---------------------------------------------------------------------------
# Plain English description
# ---------------------------------------------------------------------------

STRUCT_TEXT = {
    "BOS":        "break of structure",
    "CHOCH":      "change of character",
    "MSS":        "market structure shift",
    "PULLBACK":   "pullback / retrace",
    "RANGE":      "ranging",
    "TREND_CONT": "trend continuation",
}
ENTRY_HINTS = {
    "BOS":        "Wait for fractal HL on retrace with RVOL declining.",
    "CHOCH":      "Watch for MSS + delta flip before entering.",
    "MSS":        "Confirm with LTF CHoCH and volume spike at reversal.",
    "PULLBACK":   "Look for fractal HL at 50-62% retrace, RVOL < 0.7x.",
    "RANGE":      "Trade edges only — wait for liquidity sweep + reclaim.",
    "TREND_CONT": "Trail with fractal lows. Add on BOS confirmations.",
}
NEXT_HINT = {
    "PULLBACK":   "pullback then continuation",
    "BOS":        "momentum continuation",
    "CHOCH":      "reversal building",
    "MSS":        "structural reversal",
    "RANGE":      "sideways",
    "TREND_CONT": "trend continuation",
}

def state_to_english(state_id: str, instrument: str, next_states: list) -> str:
    parts = state_id.split("_")
    if len(parts) < 7:
        return f"{instrument}: {state_id}"
    trend  = {"BULL":"bullish","BEAR":"bearish","NEUTRAL":"neutral"}.get(parts[0], parts[0])
    mom    = {"STRONG":"strong momentum","WEAK":"weak momentum","NEUTRAL":"neutral momentum"}.get(parts[1], parts[1])
    vol    = {"HIGH":"high vol","NORMAL":"normal vol","LOW":"low vol / compression"}.get(parts[2], parts[2])
    sess   = parts[4]
    struct = parts[5] + ("_" + parts[6] if len(parts) == 8 else "")
    rsi    = parts[-1]
    s_txt  = STRUCT_TEXT.get(struct, struct)
    hint   = ENTRY_HINTS.get(struct, "Monitor for confirmation.")
    rsi_txt= {"OB":"RSI overbought","OS":"RSI oversold","NEUTRAL":""}.get(rsi,"")

    next_desc = ""
    if next_states:
        top = next_states[0]
        top_parts = top["state"].split("_")
        if len(top_parts) >= 6:
            top_struct = top_parts[5] + ("_" + top_parts[6] if len(top_parts) == 8 else "")
            next_desc = f" Top transition ({top['probability']:.0%}): {NEXT_HINT.get(top_struct,'?')}."

    rsi_part = f", {rsi_txt}" if rsi_txt else ""
    return (
        f"{instrument}: {trend} with {mom} ({vol}, {s_txt}{rsi_part}, {sess}).{next_desc} {hint}"
    )


# ---------------------------------------------------------------------------
# Webhook + WhatsApp
# ---------------------------------------------------------------------------

def build_webhook_payload(
    instrument: str, direction: str, state_id: str,
    confidence: float, ev: float, session: str,
    conviction: str, pillars: int,
) -> dict:
    size = CFG.get("prop", {}).get("normal_size", 0.10)
    return {
        "action":            "ENTER",
        "symbol":            instrument,
        "direction":         direction,
        "size":              size,
        "state":             state_id,
        "confidence":        round(confidence, 4),
        "ev":                round(ev, 4),
        "session":           session,
        "conviction":        conviction,
        "pillars_confirmed": pillars,
    }


def fire_webhook(payload: dict, dry_run: bool = True) -> bool:
    url = (CFG.get("webhook", {}).get("railway_url") or
           CFG.get("webhook", {}).get("url", ""))
    if dry_run or not url:
        log.info(f"[DRY RUN] Webhook payload: {json.dumps(payload)}")
        return True
    try:
        import requests
        resp = requests.post(
            url, json=payload,
            timeout=CFG["webhook"].get("timeout_seconds", 10),
        )
        resp.raise_for_status()
        log.info(f"Webhook fired for {payload['symbol']}: {resp.status_code}")
        return True
    except Exception as e:
        log.error(f"Webhook failed for {payload['symbol']}: {e}")
        return False


def build_whatsapp_message(
    signal: dict, instrument: str, description: str, pillars_detail: dict,
) -> str:
    now_sast = (datetime.now(timezone.utc) + SAST_OFFSET).strftime("%H:%M SAST")
    tier_icon = "[GREEN]" if signal["conviction"] in ("A+", "A") else "[AMBER]"
    pillar_hints = []
    for name, p in pillars_detail.items():
        status = "✓" if p["confirmed"] else "✗"
        pillar_hints.append(f"  {status} {name.title()}: {p['signal']}")
    pillar_str = "\n".join(pillar_hints)

    return (
        f"{tier_icon} H2 Signal — {instrument}\n"
        f"Direction: {signal['direction']}\n"
        f"State: {description}\n"
        f"Conviction: {signal['conviction']} | Pillars: {signal['pillars_confirmed']}/4\n"
        f"Probability: {signal['confidence']:.0%} | EV: {signal['ev']:+.2f}R\n"
        f"Session: {signal['session']} | {now_sast}\n"
        f"Pillar detail:\n{pillar_str}"
    )


def send_whatsapp(message: str, dry_run: bool = True) -> int:
    """
    Send a WhatsApp message via CallMeBot.
    Returns HTTP status code (200 = success).
    Number and apikey loaded from config.whatsapp.
    """
    wa_cfg = CFG.get("whatsapp", {})
    if not wa_cfg.get("enabled", False):
        log.debug("WhatsApp disabled in config")
        return 0
    if dry_run:
        log.info(f"[DRY RUN] WhatsApp suppressed:\n{message}")
        return 200   # simulate success in dry run
    try:
        import requests
        resp = requests.get(
            wa_cfg["url"],
            params={
                "phone":  wa_cfg["phone"],
                "text":   message,
                "apikey": wa_cfg["apikey"],
            },
            timeout=wa_cfg.get("timeout_seconds", 15),
        )
        log.info(f"CallMeBot: {resp.status_code} — {resp.text[:120]}")
        return resp.status_code
    except Exception as e:
        log.error(f"WhatsApp send failed: {e}")
        return 0


# ---------------------------------------------------------------------------
# Prop mode guard
# ---------------------------------------------------------------------------

def prop_mode_check(instrument: str, signal: dict, daily_pnl_r: float) -> tuple[bool, str]:
    """Returns (allowed, reason). daily_pnl_r tracked externally."""
    pc = CFG.get("prop", {})
    if daily_pnl_r <= pc.get("daily_loss_limit", -3.0):
        return False, f"daily loss limit reached ({daily_pnl_r:.1f}R)"
    return True, ""


def update_outcome(instrument: str, state_id: str, outcome_r: float):
    """Fill in outcome_r for the most recent matching row in the signal log."""
    if not SIGNAL_LOG_PATH.exists():
        return
    df = pd.read_csv(SIGNAL_LOG_PATH)
    mask = (df["instrument"] == instrument) & (df["state_id"] == state_id) & df["outcome_r"].isna()
    if mask.any():
        idx = df[mask].index[-1]
        df.loc[idx, "outcome_r"] = outcome_r
        df.to_csv(SIGNAL_LOG_PATH, index=False)
        log.info(f"Updated outcome: {instrument} {state_id} = {outcome_r:+.2f}R")


# ---------------------------------------------------------------------------
# Signal log writer
# ---------------------------------------------------------------------------

def ensure_signal_log():
    if not SIGNAL_LOG_PATH.exists():
        with open(SIGNAL_LOG_PATH, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(SIGNAL_LOG_COLS)


def append_signal_log(row: dict):
    ensure_signal_log()
    with open(SIGNAL_LOG_PATH, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=SIGNAL_LOG_COLS)
        writer.writerow({k: row.get(k, "") for k in SIGNAL_LOG_COLS})


# ---------------------------------------------------------------------------
# H2_live_state.json atomic writer
# ---------------------------------------------------------------------------

def atomic_write_live_state(data: dict):
    tmp = LIVE_STATE_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)
    tmp.replace(LIVE_STATE_PATH)


def load_live_state_or_empty() -> dict:
    if LIVE_STATE_PATH.exists():
        with open(LIVE_STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {"instruments": {}}


# ---------------------------------------------------------------------------
# Single instrument cycle
# ---------------------------------------------------------------------------

def process_instrument(
    instrument:  str,
    live_state:  dict,
    dry_run:     bool = True,
) -> tuple[dict, Optional[dict]]:
    """
    Full processing pipeline for one instrument.
    Returns (updated_instrument_entry, signal_payload_or_None).
    """
    now_utc  = datetime.now(timezone.utc)
    now_sast = now_utc + SAST_OFFSET
    session  = _current_session(now_utc.hour)

    # ── 1. Fetch bars ─────────────────────────────────────────────────────────
    df = fetch_bars(instrument, LIVE_TF, n_bars=LOOKBACK)
    if df is None or df.empty:
        log.warning(f"{instrument}: no bar data available")
        return {}, None

    df = df.sort_values("datetime_utc").reset_index(drop=True)

    # ── 2. Compute features ───────────────────────────────────────────────────
    try:
        feat_df = compute_features(df)
    except Exception as e:
        log.error(f"{instrument}: feature compute failed: {e}")
        return {}, None

    feat_row = feat_df.iloc[-1]

    # ── 3. Classify state ─────────────────────────────────────────────────────
    try:
        clf = StateClassifier()
        classified = clf.classify_dataframe(feat_df)
        state_id  = str(classified["state_id"].iloc[-1])
    except Exception as e:
        log.error(f"{instrument}: state classify failed: {e}")
        return {}, None

    # ── 4. Load transition matrix ─────────────────────────────────────────────
    matrix, raw_counts, matrix_source = load_matrix(instrument, LIVE_TF, session)

    # ── 5. Top 3 next states ─────────────────────────────────────────────────
    stats_data  = load_state_stats(instrument, LIVE_TF)
    state_stats = stats_data.get("states", {})
    dwell_times = load_dwell_times(instrument, LIVE_TF)

    next_raw = matrix.get(state_id, {})
    next_states = sorted(
        [{"state": s, "probability": p,
          "ev":    state_stats.get(s, {}).get("ev", 0.0),
          "description": state_to_english(s, instrument, [])}
         for s, p in next_raw.items()],
        key=lambda x: x["probability"], reverse=True
    )[:3]

    top_prob = next_states[0]["probability"] if next_states else 0.0
    direction = direction_from_state(state_id, next_states)

    # ── 6. Historical stats for current state ─────────────────────────────────
    ss        = state_stats.get(state_id, {})
    hist_wr   = ss.get("win_rate",        0.0)
    hist_ev   = ss.get("ev",              0.0)
    samples   = ss.get("sample_count",    0)
    stability = ss.get("stability_score", 0.0)
    ci        = [ss.get("ci_95_low", 0.0), ss.get("ci_95_high", 1.0)]
    dwell_avg = dwell_times.get(state_id, None)

    # ── 7. Gate check ─────────────────────────────────────────────────────────
    gate_checker = GateChecker()
    gates, all_gates_pass = gate_checker.check_all(
        state_id, top_prob, matrix, feat_row, session, instrument
    )

    # ── 8. Pillar scoring ─────────────────────────────────────────────────────
    next_struct = next_states[0]["state"].split("_")[5] if next_states else "UNKNOWN"
    pillar_scorer = PillarScorer()
    pillars_detail, pillars_count = pillar_scorer.score(feat_df, next_states, direction)

    # ── 9. Conviction ─────────────────────────────────────────────────────────
    conviction = {4:"A+", 3:"A", 2:"B", 1:"SKIP", 0:"SKIP"}.get(pillars_count, "SKIP")

    # ── 10. Build entry ───────────────────────────────────────────────────────
    description = state_to_english(state_id, instrument, next_states)
    ruin_pct    = stats_data.get("monte_carlo", {}).get("probability_of_ruin", 0.0) * 100

    entry = {
        "instrument":            instrument,
        "timeframe":             LIVE_TF,
        "last_bar_utc":          str(feat_df["datetime_utc"].iloc[-1]),
        "last_bar_sast":         str((pd.to_datetime(feat_df["datetime_utc"].iloc[-1], utc=True) + SAST_OFFSET)),
        "session":               session,
        "matrix_source":         matrix_source,
        "current_state":         state_id,
        "state_description":     description,
        "next_states":           next_states,
        "gates":                 gates,
        "all_gates_pass":        all_gates_pass,
        "confirmation_pillars":  pillars_detail,
        "pillars_confirmed":     pillars_count,
        "conviction":            conviction,
        "direction":             direction,
        "historical_wr":         round(hist_wr,   4),
        "historical_ev":         round(hist_ev,   4),
        "sample_count":          samples,
        "ci_95":                 ci,
        "stability_score":       round(stability, 3),
        "ruin_pct":              round(ruin_pct,  2),
        "dwell_time_avg_bars":   dwell_avg,
    }

    # ── 11. Webhook + signal log ──────────────────────────────────────────────
    fired = False
    whatsapp_sent = False
    signal_payload = None

    should_fire = (all_gates_pass and conviction in ("A+", "A") and
                   samples >= 30 and hist_ev > 0)

    if should_fire:
        signal_payload = build_webhook_payload(
            instrument, direction, state_id,
            top_prob, hist_ev, session, conviction, pillars_count
        )
        fired = fire_webhook(signal_payload, dry_run=dry_run)

        # Fan-out to users — number from whatsapp config, not per-user
        msg = build_whatsapp_message(signal_payload, instrument, description, pillars_detail)
        for user in CFG.get("users", []):
            if not user.get("active", False):
                continue
            tier = user.get("tier", "view")
            if tier in ("full", "signals"):
                status = send_whatsapp(msg, dry_run=dry_run)
                if status == 200:
                    whatsapp_sent = True

    # ── 12. Append to signal log ──────────────────────────────────────────────
    log_row = {
        "timestamp_utc":    now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sast_time":        now_sast.strftime("%Y-%m-%dT%H:%M:%S+02:00"),
        "instrument":       instrument,
        "direction":        direction,
        "state_id":         state_id,
        "confidence":       round(top_prob, 4),
        "ev":               round(hist_ev, 4),
        "session":          session,
        "pillars_confirmed":pillars_count,
        "conviction":       conviction,
        "gate_markov_gap":  gates["markov_gap"]["pass"],
        "gate_persistence": gates["markov_persistence"]["pass"],
        "gate_vol":         gates["volatility_cap"]["pass"],
        "gate_hurst":       gates["hurst"]["pass"],
        "gate_session":     gates["session"]["pass"],
        "all_gates_pass":   all_gates_pass,
        "fired":            fired,
        "whatsapp_sent":    whatsapp_sent,
        "outcome_r":        "",
    }
    append_signal_log(log_row)

    log.info(
        f"{instrument:8} | {state_id[:45]:<45} | "
        f"gates={'ALL' if all_gates_pass else 'FAIL'} | "
        f"pillars={pillars_count}/4 | conv={conviction} | fired={fired}"
    )

    return entry, signal_payload


def _current_session(utc_hour: int) -> str:
    if 13 <= utc_hour < 16: return "OVERLAP"
    if 7  <= utc_hour < 16: return "LONDON"
    if 16 <= utc_hour < 22: return "NY"
    if 0  <= utc_hour <  9: return "TOKYO"
    return "OFFHOURS"


# ---------------------------------------------------------------------------
# Full cycle
# ---------------------------------------------------------------------------

def run_cycle(dry_run: bool = True, instruments: Optional[list] = None) -> dict:
    """
    Process all instruments once.
    Returns the updated live_state dict.
    """
    now_utc  = datetime.now(timezone.utc)
    now_sast = now_utc + SAST_OFFSET
    session  = _current_session(now_utc.hour)

    insts = instruments or ALL_TIERS
    live_state = load_live_state_or_empty()
    live_state["generated_at_utc"]  = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    live_state["generated_at_sast"] = now_sast.isoformat()
    live_state["session"]            = session

    if "instruments" not in live_state:
        live_state["instruments"] = {}

    signals_fired = 0
    for inst in insts:
        try:
            entry, signal = process_instrument(inst, live_state, dry_run=dry_run)
            if entry:
                live_state["instruments"][inst] = entry
            if signal:
                signals_fired += 1
            atomic_write_live_state(live_state)
        except Exception as e:
            log.error(f"Cycle error for {inst}: {e}", exc_info=True)

    log.info(f"Cycle complete: {len(insts)} instruments | {signals_fired} signals fired")
    return live_state


# ---------------------------------------------------------------------------
# CLI + continuous loop
# ---------------------------------------------------------------------------

def print_cycle_summary(live_state: dict):
    print("\n" + "=" * 80)
    print("  H2 LIVE STATE SNAPSHOT")
    print("=" * 80)
    print(f"  Session : {live_state.get('session','?')}  | "
          f"Generated: {live_state.get('generated_at_sast','?')[:19]} SAST")
    print(f"  {'INST':<10} {'STATE':<45} {'GATES':<6} {'PIL':>5} {'CONV':<5} {'FIRED'}")
    print("  " + "-" * 78)
    for inst, info in live_state.get("instruments", {}).items():
        sid   = info.get("current_state", "?")[:44]
        gates = "ALL" if info.get("all_gates_pass") else "FAIL"
        pils  = info.get("pillars_confirmed", 0)
        conv  = info.get("conviction", "?")
        fired = "YES" if info.get("fired", False) else "no"
        print(f"  {inst:<10} {sid:<45} {gates:<6} {pils:>3}/4 {conv:<5} {fired}")
    print("=" * 80)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="H2 Quant — Live Monitor (Phase 7)")
    parser.add_argument("--once",  action="store_true", help="Run one cycle then exit")
    parser.add_argument("--tier1", action="store_true", help="Process Tier 1 only")
    parser.add_argument("--live",  action="store_true", help="Live mode — fire real webhooks")
    parser.add_argument("--instrument", "-i", help="Single instrument only")
    args = parser.parse_args()

    dry_run = not args.live
    if dry_run:
        log.info("Running in DRY RUN mode — no webhooks or WhatsApp will be sent")
    else:
        log.warning("LIVE MODE — webhooks and WhatsApp will fire if gates pass")

    insts = None
    if args.instrument:
        insts = [args.instrument]
    elif args.tier1:
        insts = TIER1

    ensure_signal_log()

    if args.once or args.tier1 or args.instrument:
        live = run_cycle(dry_run=dry_run, instruments=insts)
        print_cycle_summary(live)

        # Print signal log preview
        if SIGNAL_LOG_PATH.exists():
            df = pd.read_csv(SIGNAL_LOG_PATH)
            print(f"\nSignal log — last {min(6, len(df))} rows:")
            cols = ["timestamp_utc","instrument","state_id","conviction",
                    "all_gates_pass","pillars_confirmed","fired"]
            print(df[cols].tail(6).to_string(index=False))

        print("\nPhase 7 dry run complete. Ready for Phase 8 — Pine Script Overlay.")

    else:
        log.info(f"Starting continuous loop (interval={INTERVAL}s)")
        while True:
            t0 = time.time()
            try:
                run_cycle(dry_run=dry_run)
            except Exception as e:
                log.error(f"Cycle failed: {e}", exc_info=True)
            elapsed = time.time() - t0
            sleep_secs = max(0, INTERVAL - elapsed)
            log.info(f"Next cycle in {sleep_secs:.0f}s")
            time.sleep(sleep_secs)
