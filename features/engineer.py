"""
H2 Quant v1 — Phase 2: Feature Engineer
Computes all 22 market dimensions per bar.
Every dimension outputs: {value, confidence, reasoning}
Input:  data/processed/H2_raw_{instrument}_{tf}.parquet
Output: features/H2_features_{instrument}_{tf}.parquet
"""

import logging
import sys
import warnings
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import yaml

warnings.filterwarnings("ignore", category=RuntimeWarning)

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
        logging.FileHandler(LOG_DIR / "engineer.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("H2.engineer")

FEAT_DIR = ROOT / CFG["paths"]["features"]
FEAT_DIR.mkdir(parents=True, exist_ok=True)
PROC_DIR = ROOT / CFG["paths"]["data_processed"]

FC = CFG["features"]

# ── helpers ─────────────────────────────────────────────────────────────────

def _col(prefix: str, suffix: str) -> str:
    return f"{prefix}__{suffix}"

def _set(df: pd.DataFrame, prefix: str, value, confidence, reasoning: str):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        df[_col(prefix, "value")]      = value
        df[_col(prefix, "confidence")] = confidence
        df[_col(prefix, "reasoning")]  = reasoning

def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()

def _sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period).mean()

def _slope(series: pd.Series, n: int = 5) -> pd.Series:
    return series.diff(n) / n

def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain  = delta.clip(lower=0)
    loss  = (-delta).clip(lower=0)
    avg_g = gain.ewm(com=period - 1, adjust=False).mean()
    avg_l = loss.ewm(com=period - 1, adjust=False).mean()
    rs    = avg_g / avg_l.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def _atr(high, low, close, period: int = 14) -> pd.Series:
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low  - close.shift(1)).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(com=period - 1, adjust=False).mean()

def _swing_highs(high: pd.Series, lookback: int = 5) -> pd.Series:
    out = pd.Series(np.nan, index=high.index)
    for i in range(lookback, len(high) - lookback):
        window = high.iloc[i - lookback: i + lookback + 1]
        if high.iloc[i] == window.max():
            out.iloc[i] = high.iloc[i]
    return out

def _swing_lows(low: pd.Series, lookback: int = 5) -> pd.Series:
    out = pd.Series(np.nan, index=low.index)
    for i in range(lookback, len(low) - lookback):
        window = low.iloc[i - lookback: i + lookback + 1]
        if low.iloc[i] == window.min():
            out.iloc[i] = low.iloc[i]
    return out


# ── dimension functions ──────────────────────────────────────────────────────

def dim_price_structure(df: pd.DataFrame):
    """1. Price structure"""
    tp = (df["high"] + df["low"] + df["close"]) / 3
    lr = np.log(df["close"] / df["close"].shift(1))
    _set(df, "d01_typical_price", tp, 1.0, "typical_price=(H+L+C)/3")
    _set(df, "d01_log_return",    lr, 1.0, "log_return=ln(C/C[-1])")


def dim_volume(df: pd.DataFrame):
    """2. Volume — RVOL, delta proxy, cumulative delta"""
    vol = df["volume"].replace(0, np.nan)
    avg = vol.rolling(FC["volume_avg_period"]).mean()
    rvol = vol / avg.replace(0, np.nan)
    delta = np.where(df["close"] >= df["open"], vol, -vol)
    cum_delta = pd.Series(delta, index=df.index).fillna(0).cumsum()
    conf = (avg.notna() & (avg > 0)).astype(float)
    _set(df, "d02_rvol",       rvol.fillna(1.0), conf, f"rvol=vol/avg({FC['volume_avg_period']})")
    _set(df, "d02_delta",      pd.Series(delta, index=df.index).fillna(0), conf,
         "delta=+vol if close>=open else -vol")
    _set(df, "d02_cum_delta",  cum_delta, conf, "cumulative_delta")


def dim_volatility(df: pd.DataFrame):
    """3. Volatility — ATR, BBWidth, Garman-Klass, GARCH(1,1) approx"""
    atr = _atr(df["high"], df["low"], df["close"], FC["atr_period"])
    mid = _sma(df["close"], FC["bb_period"])
    std = df["close"].rolling(FC["bb_period"]).std()
    bb_upper = mid + FC["bb_stddev"] * std
    bb_lower = mid - FC["bb_stddev"] * std
    bb_width = (bb_upper - bb_lower) / mid.replace(0, np.nan)

    # Garman-Klass volatility estimator
    log_hl = np.log(df["high"] / df["low"].replace(0, np.nan))
    log_co = np.log(df["close"] / df["open"].replace(0, np.nan))
    gk = np.sqrt(
        _sma(0.5 * log_hl**2 - (2 * np.log(2) - 1) * log_co**2, FC["bb_period"])
    )

    # GARCH(1,1) proxy — rolling weighted variance
    ret = np.log(df["close"] / df["close"].shift(1))
    w_var = ret.ewm(span=FC["atr_period"], adjust=False).var()
    garch_vol = np.sqrt(w_var.clip(lower=0))

    n_ok = atr.notna()
    conf = n_ok.astype(float)
    _set(df, "d03_atr",      atr.fillna(0),      conf, f"ATR({FC['atr_period']})")
    _set(df, "d03_bb_width", bb_width.fillna(0), conf, f"BBWidth({FC['bb_period']},{FC['bb_stddev']})")
    _set(df, "d03_gk_vol",   gk.fillna(0),       conf, "Garman-Klass volatility")
    _set(df, "d03_garch_vol",garch_vol.fillna(0),conf, "GARCH(1,1) proxy via EWM variance")


def dim_momentum(df: pd.DataFrame):
    """4. Momentum — RSI, MACD histogram, Williams %R, CCI"""
    rsi = _rsi(df["close"], FC["rsi_period"])

    ema_fast = _ema(df["close"], FC["macd_fast"])
    ema_slow = _ema(df["close"], FC["macd_slow"])
    macd_line = ema_fast - ema_slow
    signal    = _ema(macd_line, FC["macd_signal"])
    macd_hist = macd_line - signal

    p = FC["williams_r_period"]
    highest_high = df["high"].rolling(p).max()
    lowest_low   = df["low"].rolling(p).min()
    wr = -100 * (highest_high - df["close"]) / (highest_high - lowest_low).replace(0, np.nan)

    cp = FC["cci_period"]
    tp = (df["high"] + df["low"] + df["close"]) / 3
    tp_sma  = tp.rolling(cp).mean()
    mean_dev = tp.rolling(cp).apply(lambda x: np.mean(np.abs(x - x.mean())), raw=True)
    cci = (tp - tp_sma) / (0.015 * mean_dev.replace(0, np.nan))

    conf_rsi  = rsi.notna().astype(float)
    conf_macd = macd_hist.notna().astype(float)
    conf_wr   = wr.notna().astype(float)
    conf_cci  = cci.notna().astype(float)

    _set(df, "d04_rsi",       rsi.fillna(50),       conf_rsi,  f"RSI({FC['rsi_period']})")
    _set(df, "d04_macd_hist", macd_hist.fillna(0),  conf_macd, "MACD histogram")
    _set(df, "d04_williams_r",wr.fillna(-50),        conf_wr,   f"Williams%R({FC['williams_r_period']})")
    _set(df, "d04_cci",       cci.fillna(0),         conf_cci,  f"CCI({FC['cci_period']})")


def dim_liquidity(df: pd.DataFrame):
    """5. Liquidity — distance to swing H/L, FVG, imbalance"""
    lb = FC["fractal_lookback"]
    swing_h = _swing_highs(df["high"], lb).ffill()
    swing_l = _swing_lows(df["low"],   lb).ffill()

    dist_to_high = (swing_h - df["close"]) / df["close"].replace(0, np.nan)
    dist_to_low  = (df["close"] - swing_l) / df["close"].replace(0, np.nan)

    # FVG: gap between bar[-2].high and bar[0].low (bull FVG) or bar[-2].low and bar[0].high (bear FVG)
    fvg_bull = df["low"] > df["high"].shift(2)
    fvg_bear = df["high"] < df["low"].shift(2)
    fvg_flag = (fvg_bull | fvg_bear).astype(float)

    # Imbalance: body > 70% of range
    body   = (df["close"] - df["open"]).abs()
    rng    = (df["high"] - df["low"]).replace(0, np.nan)
    imbal  = (body / rng > 0.70).astype(float)

    conf = (swing_h.notna() & swing_l.notna()).astype(float)
    _set(df, "d05_dist_swing_high", dist_to_high.fillna(0), conf, "dist=(swingH-close)/close")
    _set(df, "d05_dist_swing_low",  dist_to_low.fillna(0),  conf, "dist=(close-swingL)/close")
    _set(df, "d05_fvg_flag",        fvg_flag,               1.0,  "FVG: gap between bar[-2] and bar[0]")
    _set(df, "d05_imbalance_flag",  imbal,                  1.0,  "imbalance: body/range > 0.70")


def dim_time(df: pd.DataFrame):
    """6. Time — session, SAST hour, day-of-week, kill zone"""
    sast = df["datetime_sast"]
    hour_utc  = df["datetime_utc"].dt.hour
    hour_sast = sast.dt.hour
    dow       = sast.dt.dayofweek  # 0=Mon

    s = CFG["sessions"]
    def _session(h):
        if s["overlap"]["start_utc"][:2] <= str(h).zfill(2) < s["overlap"]["end_utc"][:2]:
            return "OVERLAP"
        if s["london"]["start_utc"][:2] <= str(h).zfill(2) < s["london"]["end_utc"][:2]:
            return "LONDON"
        if s["ny"]["start_utc"][:2] <= str(h).zfill(2) < s["ny"]["end_utc"][:2]:
            return "NY"
        if str(h).zfill(2) < s["tokyo"]["end_utc"][:2]:
            return "TOKYO"
        return "OFFHOURS"

    session_label = hour_utc.apply(_session)

    # Kill zones (UTC): London open 07-09, NY open 13-15, NY close 19-20
    kill_zone = ((hour_utc >= 7)  & (hour_utc < 9)  |
                 (hour_utc >= 13) & (hour_utc < 15) |
                 (hour_utc >= 19) & (hour_utc < 20)).astype(float)

    _set(df, "d06_session",    session_label,  1.0, "session_label from UTC hour")
    _set(df, "d06_hour_sast",  hour_sast,      1.0, "hour in SAST (UTC+2)")
    _set(df, "d06_dow",        dow,            1.0, "day_of_week 0=Mon")
    _set(df, "d06_kill_zone",  kill_zone,      1.0, "kill_zone: London/NY open+close")


def dim_trend(df: pd.DataFrame):
    """7. Trend — EMA 21/50/200 alignment"""
    periods = FC["ema_periods"]  # [21, 50, 200]
    e21  = _ema(df["close"], periods[0])
    e50  = _ema(df["close"], periods[1])
    e200 = _ema(df["close"], periods[2])

    bull = (e21 > e50) & (e50 > e200) & (df["close"] > e21)
    bear = (e21 < e50) & (e50 < e200) & (df["close"] < e21)
    label = pd.Series("NEUTRAL", index=df.index)
    label[bull] = "BULL"
    label[bear] = "BEAR"

    # Confidence: 1.0 once EMA200 has warmed up (200 bars), else proportional
    warmup = pd.Series(np.minimum(np.arange(len(df)) / 200.0, 1.0), index=df.index)
    conf = e200.notna().astype(float) * warmup

    _set(df, "d07_trend",      label,           conf, f"EMA{periods[0]}/EMA{periods[1]}/EMA{periods[2]} alignment")
    _set(df, "d07_ema21",      e21.fillna(df["close"]), conf, f"EMA({periods[0]})")
    _set(df, "d07_ema50",      e50.fillna(df["close"]), conf, f"EMA({periods[1]})")
    _set(df, "d07_ema200",     e200.fillna(df["close"]),conf, f"EMA({periods[2]})")


def dim_market_structure(df: pd.DataFrame):
    """8. Market structure — HH/HL/LH/LL, BOS, CHoCH, MSS"""
    lb = FC["fractal_lookback"]
    sh = _swing_highs(df["high"], lb)
    sl = _swing_lows(df["low"],   lb)

    # Track sequence of swing highs and lows
    sh_vals = sh.dropna()
    sl_vals = sl.dropna()

    struct = pd.Series("UNKNOWN", index=df.index)
    bos    = pd.Series(0.0, index=df.index)
    choch  = pd.Series(0.0, index=df.index)
    mss    = pd.Series(0.0, index=df.index)

    prev_sh = np.nan
    prev_sl = np.nan
    direction = "UNKNOWN"

    for i in range(len(df)):
        idx = df.index[i]
        cur_high = df["high"].iloc[i]
        cur_low  = df["low"].iloc[i]
        cur_close = df["close"].iloc[i]

        # Detect BOS: close breaks prior swing high/low in trend direction
        if not np.isnan(prev_sh) and not np.isnan(prev_sl):
            if cur_close > prev_sh:  # bullish BOS
                if direction == "BEAR":
                    choch.iloc[i] = 1.0  # CHoCH: was bearish, now breaking up
                    direction = "BULL"
                bos.iloc[i] = 1.0
                struct.iloc[i] = "BOS"
            elif cur_close < prev_sl:  # bearish BOS
                if direction == "BULL":
                    choch.iloc[i] = 1.0
                    direction = "BEAR"
                bos.iloc[i] = 1.0
                struct.iloc[i] = "BOS"

        # MSS: CHoCH + BOS in same region (simplified: CHoCH confirmed by next BOS)
        if i > 0 and choch.iloc[i - 1] == 1.0 and bos.iloc[i] == 1.0:
            mss.iloc[i] = 1.0
            struct.iloc[i] = "MSS"

        # Update swing references
        if not np.isnan(sh.iloc[i] if i < len(sh) else np.nan):
            if np.isnan(prev_sh) or sh.iloc[i] != prev_sh:
                prev_sh = sh.iloc[i]
        if not np.isnan(sl.iloc[i] if i < len(sl) else np.nan):
            if np.isnan(prev_sl) or sl.iloc[i] != prev_sl:
                prev_sl = sl.iloc[i]

    # HH/HL/LH/LL via consecutive swing comparison
    hh_hl_series = pd.Series("UNKNOWN", index=df.index)
    prev_h = np.nan
    prev_l = np.nan
    for i, idx in enumerate(df.index):
        if not np.isnan(sh.iloc[i]):
            if not np.isnan(prev_h):
                hh_hl_series.iloc[i] = "HH" if sh.iloc[i] > prev_h else "LH"
            prev_h = sh.iloc[i]
        if not np.isnan(sl.iloc[i]):
            if not np.isnan(prev_l):
                hh_hl_series.iloc[i] = "HL" if sl.iloc[i] > prev_l else "LL"
            prev_l = sl.iloc[i]

    conf = (sh.notna() | sl.notna()).astype(float)
    _set(df, "d08_structure",  struct,      conf, "BOS/CHoCH/MSS from swing H/L breaks")
    _set(df, "d08_bos",        bos,         conf, "BOS flag")
    _set(df, "d08_choch",      choch,       conf, "CHoCH flag")
    _set(df, "d08_mss",        mss,         conf, "MSS flag")
    _set(df, "d08_hh_hl",      hh_hl_series,conf, "HH/HL/LH/LL classification")


def dim_order_flow(df: pd.DataFrame):
    """9. Order flow — delta proxy, session cumulative delta"""
    vol = df["volume"].replace(0, np.nan)
    delta = pd.Series(
        np.where(df["close"] >= df["open"], vol, -vol),
        index=df.index
    ).fillna(0)

    # Session cumulative delta: reset each session
    session = df.get("d06_session__value", df["datetime_utc"].dt.hour.apply(
        lambda h: "NY" if h >= 13 else ("LONDON" if h >= 7 else "TOKYO")))
    session_delta = delta.copy()
    session_group = (session != session.shift(1)).cumsum()
    session_delta = delta.groupby(session_group).cumsum()

    conf = vol.notna().astype(float)
    _set(df, "d09_delta",         delta,         conf, "delta proxy: +vol buy / -vol sell")
    _set(df, "d09_session_delta", session_delta, conf, "cumulative delta per session")


def dim_market_profile(df: pd.DataFrame):
    """10. Market profile — VWAP deviation, session POC distance"""
    tp = (df["high"] + df["low"] + df["close"]) / 3
    vol = df["volume"].replace(0, 1)

    # VWAP: rolling daily (reset at session open — approximate with 390-bar rolling for intraday)
    # For daily/4H data, use cumulative from start of dataframe
    cum_tpv = (tp * vol).cumsum()
    cum_v   = vol.cumsum()
    vwap    = cum_tpv / cum_v
    vwap_dev = (df["close"] - vwap) / vwap.replace(0, np.nan)

    # POC: price with highest volume in rolling 20-bar window (simplified)
    poc_dist = pd.Series(np.nan, index=df.index)
    w = 20
    for i in range(w, len(df)):
        seg = df.iloc[i - w: i]
        seg_vol = seg["volume"].values
        seg_tp  = ((seg["high"] + seg["low"] + seg["close"]) / 3).values
        if seg_vol.sum() > 0:
            poc_price = seg_tp[np.argmax(seg_vol)]
            poc_dist.iloc[i] = (df["close"].iloc[i] - poc_price) / df["close"].iloc[i]

    conf_vwap = vwap.notna().astype(float)
    conf_poc  = poc_dist.notna().astype(float)
    _set(df, "d10_vwap_dev",  vwap_dev.fillna(0),  conf_vwap, "VWAP deviation = (close-VWAP)/VWAP")
    _set(df, "d10_poc_dist",  poc_dist.fillna(0),  conf_poc,  "POC distance from 20-bar volume profile")


def dim_volatility_regime(df: pd.DataFrame):
    """11. Volatility regime — ATR percentile rank over 50 bars"""
    n = FC["atr_percentile_bars"]
    atr = _atr(df["high"], df["low"], df["close"], FC["atr_period"])
    rank = atr.rolling(n).apply(
        lambda x: pd.Series(x).rank(pct=True).iloc[-1], raw=False
    )
    regime = pd.Series("NORMAL", index=df.index)
    regime[rank >= 0.75] = "HIGH"
    regime[rank <= 0.25] = "LOW"

    conf = rank.notna().astype(float)
    _set(df, "d11_vol_regime",     regime,          conf, f"ATR percentile rank over {n} bars")
    _set(df, "d11_vol_rank",       rank.fillna(0.5),conf, "ATR percentile rank 0-1")


def dim_sentiment(df: pd.DataFrame):
    """12. Sentiment — Williams VIX Fix (fear proxy)"""
    p  = FC["vwf_period"]    # 22
    sm = FC["vwf_smoothing"] # 9
    highest_close = df["close"].rolling(p).max()
    wvf = (highest_close - df["low"]) / highest_close.replace(0, np.nan) * 100
    wvf_smooth = _sma(wvf, sm)
    wvf_rank = wvf.rolling(p * 2).apply(
        lambda x: pd.Series(x).rank(pct=True).iloc[-1], raw=False
    )
    conf = wvf.notna().astype(float)
    _set(df, "d12_wvf",       wvf.fillna(0),        conf, f"Williams VIX Fix({p})")
    _set(df, "d12_wvf_smooth",wvf_smooth.fillna(0), conf, f"WVF smoothed({sm})")
    _set(df, "d12_wvf_rank",  wvf_rank.fillna(0.5), conf, "WVF percentile rank")


def dim_correlation(df: pd.DataFrame, dxy_df: Optional[pd.DataFrame] = None):
    """13. Correlation — rolling 20-bar correlation to DXY proxy"""
    n = FC["volume_avg_period"]  # 20
    ret = np.log(df["close"] / df["close"].shift(1))

    if dxy_df is not None and not dxy_df.empty:
        dxy_ret = np.log(dxy_df["close"] / dxy_df["close"].shift(1))
        # Align on index
        aligned = pd.concat([ret, dxy_ret], axis=1, join="inner")
        aligned.columns = ["instr", "dxy"]
        corr = aligned["instr"].rolling(n).corr(aligned["dxy"])
        corr = corr.reindex(df.index).fillna(0)
        conf = corr.notna().astype(float)
        reasoning = f"rolling {n}-bar corr to DXY proxy ({CFG['features']['dxy_proxy']})"
    else:
        # No DXY available — use autocorrelation as proxy
        corr = ret.rolling(n).corr(ret.shift(1))
        conf = corr.notna().astype(float) * 0.3  # low confidence without true DXY
        reasoning = "autocorr lag-1 (DXY proxy unavailable)"

    _set(df, "d13_dxy_corr", corr.fillna(0), conf, reasoning)


def dim_efficiency(df: pd.DataFrame):
    """14. Efficiency — Kaufman Efficiency Ratio, Hurst exponent"""
    n_er = FC["efficiency_period"]   # 10
    n_h  = FC["hurst_period"]        # 50

    # Kaufman Efficiency Ratio
    direction = (df["close"] - df["close"].shift(n_er)).abs()
    volatility = df["close"].diff().abs().rolling(n_er).sum()
    er = direction / volatility.replace(0, np.nan)

    # Hurst exponent via RS analysis (simplified rolling)
    def _hurst(x):
        if len(x) < 20 or np.std(x) == 0:
            return 0.5
        try:
            lags = [2, 4, 8, 16]
            rs_vals = []
            for lag in lags:
                if lag >= len(x):
                    continue
                sub = x[-lag:]
                mean_sub = np.mean(sub)
                deviations = np.cumsum(sub - mean_sub)
                r = deviations.max() - deviations.min()
                s = np.std(sub, ddof=1)
                if s > 0:
                    rs_vals.append((lag, r / s))
            if len(rs_vals) < 2:
                return 0.5
            log_lags = np.log([r[0] for r in rs_vals])
            log_rs   = np.log([r[1] for r in rs_vals])
            h, _ = np.polyfit(log_lags, log_rs, 1)
            return float(np.clip(h, 0.0, 1.0))
        except Exception:
            return 0.5

    hurst = df["close"].rolling(n_h).apply(_hurst, raw=True)

    conf_er = er.notna().astype(float)
    conf_h  = hurst.notna().astype(float)
    _set(df, "d14_efficiency_ratio", er.fillna(0.5),    conf_er, f"Kaufman ER({n_er})")
    _set(df, "d14_hurst",            hurst.fillna(0.5), conf_h,  f"Hurst exponent({n_h})")


def dim_range(df: pd.DataFrame):
    """15. Range — daily range vs 20-day avg, IB range, range position %"""
    rng    = df["high"] - df["low"]
    avg_rng = rng.rolling(20).mean()
    rng_ratio = rng / avg_rng.replace(0, np.nan)

    # Range position: where does close sit in today's range
    rng_pos = (df["close"] - df["low"]) / rng.replace(0, np.nan)

    # IB range: first 2 bars of session as initial balance proxy
    session_col = df.get("d06_session__value")
    if session_col is not None:
        ib_high = pd.Series(np.nan, index=df.index)
        ib_low  = pd.Series(np.nan, index=df.index)
        grp = (session_col != session_col.shift(1)).cumsum()
        for gid, seg in df.groupby(grp):
            if len(seg) >= 2:
                ib_h = seg["high"].iloc[:2].max()
                ib_l = seg["low"].iloc[:2].min()
                ib_high[seg.index] = ib_h
                ib_low[seg.index]  = ib_l
        ib_rng = (ib_high - ib_low).fillna(0)
    else:
        ib_rng = pd.Series(0.0, index=df.index)

    conf = avg_rng.notna().astype(float)
    _set(df, "d15_range_ratio",  rng_ratio.fillna(1.0), conf, "daily_range/avg_range(20)")
    _set(df, "d15_range_pos",    rng_pos.fillna(0.5),   1.0,  "(close-low)/range")
    _set(df, "d15_ib_range",     ib_rng,                0.6,  "IB range: first 2 bars of session")


def dim_imbalance(df: pd.DataFrame):
    """16. Imbalance — FVG above/below, single print"""
    fvg_bull = df["low"] > df["high"].shift(2)
    fvg_bear = df["high"] < df["low"].shift(2)

    fvg_above = fvg_bull.astype(float)   # gap above = price traded lower, gap above current
    fvg_below = fvg_bear.astype(float)

    # Single print: wick that is not revisited in next 3 bars
    upper_wick = df["high"] - df[["open", "close"]].max(axis=1)
    lower_wick = df[["open", "close"]].min(axis=1) - df["low"]
    single_upper = (upper_wick > 0) & (df["high"] > df["high"].shift(-1)) & \
                   (df["high"] > df["high"].shift(-2)) & (df["high"] > df["high"].shift(-3))
    single_lower = (lower_wick > 0) & (df["low"] < df["low"].shift(-1)) & \
                   (df["low"] < df["low"].shift(-2)) & (df["low"] < df["low"].shift(-3))
    single_print = (single_upper | single_lower).astype(float)

    _set(df, "d16_fvg_above",    fvg_above,   1.0, "FVG above: bar[0].low > bar[-2].high")
    _set(df, "d16_fvg_below",    fvg_below,   1.0, "FVG below: bar[0].high < bar[-2].low")
    _set(df, "d16_single_print", single_print,0.7,  "single_print: wick not revisited 3 bars")


def dim_fractality(df: pd.DataFrame):
    """17. Fractals — Williams fractal H/L, nested"""
    lb = FC["fractal_lookback"]  # 5
    frac_h = _swing_highs(df["high"], lb)
    frac_l = _swing_lows(df["low"],   lb)

    # Nested fractals: fractal within a fractal (lb//2)
    nested_lb = max(2, lb // 2)
    nested_h = _swing_highs(df["high"], nested_lb)
    nested_l = _swing_lows(df["low"],   nested_lb)
    nested = (nested_h.notna() & frac_h.notna()) | (nested_l.notna() & frac_l.notna())

    frac_h_flag = frac_h.notna().astype(float)
    frac_l_flag = frac_l.notna().astype(float)

    _set(df, "d17_fractal_high",   frac_h.where(frac_h.notna(), 0), frac_h_flag, f"Williams fractal high lb={lb}")
    _set(df, "d17_fractal_low",    frac_l.where(frac_l.notna(), 0), frac_l_flag, f"Williams fractal low lb={lb}")
    _set(df, "d17_nested_fractal", nested.astype(float),            0.7,         "nested fractal (lb//2 within lb)")


# dim 18 (breadth) and dim 19 (open interest) are skipped per CLAUDE.md


def dim_accum_dist(df: pd.DataFrame):
    """20. Accumulation/Distribution — OBV slope, A/D line slope"""
    # OBV
    sign = np.where(df["close"] > df["close"].shift(1), 1,
           np.where(df["close"] < df["close"].shift(1), -1, 0))
    obv = pd.Series(sign * df["volume"].values, index=df.index).cumsum()
    obv_slope = _slope(obv, 5)

    # A/D line
    clv = ((df["close"] - df["low"]) - (df["high"] - df["close"])) / \
          (df["high"] - df["low"]).replace(0, np.nan)
    ad = (clv * df["volume"]).cumsum()
    ad_slope = _slope(ad, 5)

    conf = df["volume"].notna().astype(float)
    _set(df, "d20_obv_slope", obv_slope.fillna(0), conf, "OBV slope(5)")
    _set(df, "d20_ad_slope",  ad_slope.fillna(0),  conf, "A/D line slope(5)")


def dim_risk(df: pd.DataFrame):
    """21. Risk — ATR-based stop distance, R-multiple zones"""
    atr = _atr(df["high"], df["low"], df["close"], FC["atr_period"])

    # Stop distance: 1.5x ATR below/above close
    stop_long  = df["close"] - 1.5 * atr
    stop_short = df["close"] + 1.5 * atr

    # R-multiple zones: 1R, 2R, 3R targets
    target_1r_long  = df["close"] + 1.5 * atr
    target_2r_long  = df["close"] + 3.0 * atr
    target_3r_long  = df["close"] + 4.5 * atr

    # Stop distance as % of close
    stop_dist_pct = (1.5 * atr) / df["close"].replace(0, np.nan)

    conf = atr.notna().astype(float)
    _set(df, "d21_stop_dist_pct",   stop_dist_pct.fillna(0),  conf, "stop_dist=1.5*ATR/close")
    _set(df, "d21_stop_long",       stop_long.fillna(0),      conf, "stop_long=close-1.5*ATR")
    _set(df, "d21_stop_short",      stop_short.fillna(0),     conf, "stop_short=close+1.5*ATR")
    _set(df, "d21_target_1r_long",  target_1r_long.fillna(0), conf, "1R target long=close+1.5*ATR")
    _set(df, "d21_target_2r_long",  target_2r_long.fillna(0), conf, "2R target long=close+3*ATR")
    _set(df, "d21_target_3r_long",  target_3r_long.fillna(0), conf, "3R target long=close+4.5*ATR")


def dim_mtf_context(df: pd.DataFrame, htf_states: Optional[dict] = None):
    """22. MTF context — inject 4H and 1H state labels"""
    if htf_states:
        state_4h = htf_states.get("4H", pd.Series("UNKNOWN", index=df.index))
        state_1h = htf_states.get("1H", pd.Series("UNKNOWN", index=df.index))
    else:
        state_4h = pd.Series("UNKNOWN", index=df.index)
        state_1h = pd.Series("UNKNOWN", index=df.index)

    conf = 0.0 if htf_states is None else 0.9
    _set(df, "d22_state_4h", state_4h, conf, "4H state label (injected from HTF)")
    _set(df, "d22_state_1h", state_1h, conf, "1H state label (injected from HTF)")


# ── main compute pipeline ────────────────────────────────────────────────────

DIMENSION_PREFIXES = [
    "d01", "d02", "d03", "d04", "d05", "d06", "d07", "d08",
    "d09", "d10", "d11", "d12", "d13", "d14", "d15", "d16",
    "d17", "d20", "d21", "d22",
]

def compute_features(
    df: pd.DataFrame,
    dxy_df: Optional[pd.DataFrame] = None,
    htf_states: Optional[dict] = None,
) -> pd.DataFrame:
    """Run all 22 dimensions on a cleaned OHLCV DataFrame."""
    out = df.copy()

    dim_price_structure(out)
    dim_volume(out)
    dim_volatility(out)
    dim_momentum(out)
    dim_liquidity(out)
    dim_time(out)
    dim_trend(out)
    dim_market_structure(out)
    dim_order_flow(out)
    dim_market_profile(out)
    dim_volatility_regime(out)
    dim_sentiment(out)
    dim_correlation(out, dxy_df)
    dim_efficiency(out)
    dim_range(out)
    dim_imbalance(out)
    dim_fractality(out)
    # d18 breadth — skipped
    # d19 open interest — skipped
    dim_accum_dist(out)
    dim_risk(out)
    dim_mtf_context(out, htf_states)

    return out


# ── validator ────────────────────────────────────────────────────────────────

class FeatureValidator:
    def __init__(self, low_conf_threshold: float = 0.5, low_conf_bar_pct: float = 0.20):
        self.low_conf_threshold = low_conf_threshold
        self.low_conf_bar_pct   = low_conf_bar_pct

    def validate(self, df: pd.DataFrame, instrument: str, timeframe: str) -> dict:
        conf_cols = [c for c in df.columns if c.endswith("__confidence")]
        val_cols  = [c for c in df.columns if c.endswith("__value")]
        reas_cols = [c for c in df.columns if c.endswith("__reasoning")]

        # Check all prefixes present — col names are like d01_typical_price__value
        found_dim_prefixes = set(c.split("__")[0].split("_")[0] + c.split("__")[0].split("_")[1]
                                 if len(c.split("__")[0].split("_")) > 1 else c.split("__")[0]
                                 for c in val_cols)
        # Normalise: extract leading token like "d01", "d02", etc.
        found_dims = set()
        for c in val_cols:
            token = c.split("__")[0]          # e.g. "d01_typical_price"
            parts = token.split("_")
            if parts[0].startswith("d") and len(parts[0]) == 3:
                found_dims.add(parts[0])       # "d01"
        missing    = [p for p in DIMENSION_PREFIXES if p not in found_dims]

        warnings_list = []
        coverage = {}

        for cc in conf_cols:
            prefix  = cc.replace("__confidence", "")
            series  = pd.to_numeric(df[cc], errors="coerce")
            total   = len(series)
            low_pct = (series < self.low_conf_threshold).sum() / max(total, 1)
            coverage[prefix] = round(1.0 - low_pct, 3)
            if low_pct > self.low_conf_bar_pct:
                warnings_list.append(
                    f"{prefix}: {low_pct:.1%} bars below confidence {self.low_conf_threshold}"
                )

        report = {
            "instrument":  instrument,
            "timeframe":   timeframe,
            "total_bars":  len(df),
            "dimensions_found": len(found_dims),
            "dimensions_missing": missing,
            "warnings": warnings_list,
            "coverage": coverage,
        }

        if missing:
            log.warning(f"{instrument} {timeframe}: missing dimensions {missing}")
        for w in warnings_list:
            log.warning(f"{instrument} {timeframe}: {w}")

        return report

    def print_report(self, report: dict):
        inst = report["instrument"]
        tf   = report["timeframe"]
        print(f"\n{'='*60}")
        print(f"  Feature Validation — {inst} {tf}  ({report['total_bars']} bars)")
        print(f"{'='*60}")
        print(f"  Dimensions found:   {report['dimensions_found']}")
        if report["dimensions_missing"]:
            print(f"  MISSING:            {report['dimensions_missing']}")
        print(f"\n  Coverage per feature group:")
        for k, v in sorted(report["coverage"].items()):
            filled = int(v * 20)
            bar = "#" * filled + "-" * (20 - filled)
            print(f"    {k:<35} [{bar}]  {v:.1%}")
        if report["warnings"]:
            print(f"\n  Warnings ({len(report['warnings'])}):")
            for w in report["warnings"]:
                print(f"    [!] {w}")
        else:
            print(f"\n  No low-confidence warnings.")
        print(f"{'='*60}\n")


# ── I/O ─────────────────────────────────────────────────────────────────────

def load_raw(instrument: str, timeframe: str) -> Optional[pd.DataFrame]:
    path = ROOT / CFG["paths"]["data_processed"] / f"H2_raw_{instrument}_{timeframe}.parquet"
    if not path.exists():
        log.error(f"Raw parquet not found: {path}")
        return None
    return pd.read_parquet(path)


def save_features(df: pd.DataFrame, instrument: str, timeframe: str) -> Path:
    out = FEAT_DIR / f"H2_features_{instrument}_{timeframe}.parquet"
    table = pa.Table.from_pandas(df, preserve_index=False)
    pq.write_table(table, out, compression="snappy")
    log.info(f"Saved: {out} ({len(df)} rows, {len(df.columns)} columns)")
    return out


def engineer(
    instrument: str,
    timeframe: str,
    dxy_instrument: Optional[str] = None,
    htf_states: Optional[dict] = None,
) -> Optional[pd.DataFrame]:
    """Full pipeline: load → compute → validate → save."""
    df = load_raw(instrument, timeframe)
    if df is None:
        return None

    # Load DXY proxy if requested
    dxy_df = None
    if dxy_instrument:
        dxy_df = load_raw(dxy_instrument, timeframe)

    log.info(f"Computing features: {instrument} {timeframe} ({len(df)} bars)")
    feat_df = compute_features(df, dxy_df=dxy_df, htf_states=htf_states)

    validator = FeatureValidator()
    report    = validator.validate(feat_df, instrument, timeframe)
    validator.print_report(report)

    save_features(feat_df, instrument, timeframe)
    return feat_df


# ── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="H2 Quant — Feature Engineer (Phase 2)")
    parser.add_argument("--instrument", "-i", required=True)
    parser.add_argument("--timeframe",  "-t", required=True)
    parser.add_argument("--dxy",        help="DXY proxy instrument (e.g. USDJPY)")
    args = parser.parse_args()

    result = engineer(args.instrument, args.timeframe, dxy_instrument=args.dxy)
    if result is None:
        print("Failed — check logs/engineer.log")
        sys.exit(1)
    print(f"\nDone. {len(result)} bars × {len(result.columns)} columns")
