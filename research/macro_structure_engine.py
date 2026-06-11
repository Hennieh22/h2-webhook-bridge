"""
research/macro_structure_engine.py
===================================
Golden Highway Architecture v3 — Macro Structure Detection Engine

The v2 system asked: "Is the market bullish or bearish?"
This system asks:   "What structure is the market building,
                     where are we inside it, and what liquidity
                     is it seeking?"

SEVEN STRUCTURAL STATES
───────────────────────
  EXPANSION       — Trending impulse in macro direction
  PULLBACK        — Counter-trend retracement inside trend structure
  REACCUMULATION  — Low-volatility compression above BOS (re-loading)
  DISTRIBUTION    — Approaching external liquidity with exhaustion signs
  LIQ_SWEEP       — Fractal violated then recovered (stop hunt / inducement)
  RANGE           — No structural direction, price oscillating
  TREND_FAILURE   — CHoCH / macro structure broken

COLUMN MAP (from build_features output)
────────────────────────────────────────
  micro       = RSI cycle completion index 0-100  (formerly "col 96")
  flow_score  = multi-TF momentum slope sum -5..+5 (formerly "col 98")
  vol_ratio   = volume / 20-bar average
  atr14       = ATR(14) on current TF
  rsi_base    = RSI(14) on current TF
  rsi_1d      = Daily RSI
  htf_align   = HTF alignment score -2..+2
  ema50_4h    = 4H EMA50 (HTF trend line)
  last_pivot_high / last_pivot_low   = most recent confirmed pivot
  prior_pivot_high / prior_pivot_low = prior pivot (external target proxy)

  CVD divergence — approximated from:
    price_at_high = close > 10-bar rolling high (bull)
    rsi_lower     = rsi_base < RSI 10 bars ago
    Both true simultaneously = CVD divergence signal

Architecture dependencies:
  - build_features()  from golden_highway_engine.py
  - No additional data required (all from existing parquet)

Author: H2 Systematic Trading
"""

from __future__ import annotations

import sys, warnings
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Import golden_highway_engine FIRST — its module-level code wraps sys.stdout
# for UTF-8 output. All subsequent print() calls go through that wrapper.
from golden_highway_engine import build_features  # noqa: E402  (stdout wrap side-effect)
from structure_engine import f_has_volume

# ══════════════════════════════════════════════════════════════════════════════
# 1. STATE AND LOCATION CONSTANTS
# ══════════════════════════════════════════════════════════════════════════════

STATE_EXPANSION    = "EXPANSION"
STATE_PULLBACK     = "PULLBACK"
STATE_REACCUM      = "REACCUMULATION"
STATE_DISTRIBUTION = "DISTRIBUTION"
STATE_LIQ_SWEEP    = "LIQ_SWEEP"
STATE_RANGE        = "RANGE"
STATE_TREND_FAIL   = "TREND_FAILURE"
STATE_UNKNOWN      = "UNKNOWN"

# Location within PULLBACK (driven by micro completion 0-100)
LOC_EARLY_PB    = "EARLY_PULLBACK"      # micro < 33
LOC_MID_PB      = "MID_PULLBACK"        # micro 33-66
LOC_LATE_PB     = "LATE_PULLBACK"       # micro 66-82
LOC_COMPLETE_PB = "PULLBACK_COMPLETE"   # micro > 82 + sequence evidence

# Location within EXPANSION (driven by progress to ext_target)
LOC_EARLY_EXP   = "EARLY_EXPANSION"     # < 33% to target
LOC_MID_EXP     = "MID_EXPANSION"       # 33-66% to target
LOC_LATE_EXP    = "LATE_EXPANSION"      # > 66% to target

LOC_REACCUM_CONF = "REACCUMULATION_CONFIRMED"
LOC_SWEEP_LOW    = "SWEEP_AT_LOW"        # LIQ_SWEEP at pullback bottom
LOC_SWEEP_HIGH   = "SWEEP_AT_HIGH"       # LIQ_SWEEP at expansion top
LOC_UNKNOWN      = "UNKNOWN"

# BOS quality multipliers by location at time of BOS
# Applied to entry score: higher = better setup
BOS_QUALITY_MULT: dict[str, float] = {
    LOC_COMPLETE_PB:  1.5,   # Pullback fully confirmed — highest quality
    LOC_LATE_PB:      1.4,   # Late pullback — very good
    LOC_REACCUM_CONF: 1.3,   # Reaccum confirmed — excellent
    LOC_EARLY_EXP:    1.2,   # Continuation — good
    LOC_SWEEP_LOW:    1.3,   # Sweep at low then BOS — strong signal
    LOC_MID_PB:       0.9,   # Mid-pullback — wait for more confirmation
    LOC_EARLY_PB:     0.6,   # Too early in pullback
    LOC_MID_EXP:      0.7,   # Mid-expansion — reduced
    LOC_LATE_EXP:     0.3,   # Late expansion — do not enter
    LOC_SWEEP_HIGH:   0.5,   # Sweep at top — risky
    LOC_UNKNOWN:      0.7,
}

# Entry quality grade thresholds
ENTRY_THRESH = {"A+": 80, "A": 65, "B": 50, "C": 35}


# ══════════════════════════════════════════════════════════════════════════════
# 2. UTILITY HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _safe(arr: np.ndarray, i: int, default: float = np.nan) -> float:
    """Safe array access with nan guard."""
    if i < 0 or i >= len(arr):
        return default
    v = float(arr[i])
    return default if np.isnan(v) else v


def _rolling_max(arr: np.ndarray, window: int) -> np.ndarray:
    return pd.Series(arr).rolling(window, min_periods=1).max().values


def _rolling_min(arr: np.ndarray, window: int) -> np.ndarray:
    return pd.Series(arr).rolling(window, min_periods=1).min().values


def _rolling_mean(arr: np.ndarray, window: int) -> np.ndarray:
    return pd.Series(arr).rolling(window, min_periods=1).mean().values


def _rsi_divergence(closes: np.ndarray, rsi: np.ndarray, i: int,
                    lookback: int = 10, direction: int = 1) -> bool:
    """
    Simple divergence approximation — no CVD column needed.
    Bull divergence: price lower low, RSI higher low.
    Bear divergence: price higher high, RSI lower high.
    """
    if i < lookback:
        return False
    if direction == 1:
        price_ll = closes[i] < np.min(closes[i - lookback:i])
        rsi_hl   = rsi[i]   > np.min(rsi[i - lookback:i])
        return bool(price_ll and rsi_hl)
    else:
        price_hh = closes[i] > np.max(closes[i - lookback:i])
        rsi_lh   = rsi[i]   < np.max(rsi[i - lookback:i])
        return bool(price_hh and rsi_lh)


def _volume_climax(vol_ratio: np.ndarray, i: int,
                   lookback: int = 20, threshold: float = 1.5) -> bool:
    """True if current bar is a volume climax vs recent average."""
    return bool(_safe(vol_ratio, i, 1.0) >= threshold)


# ══════════════════════════════════════════════════════════════════════════════
# 3. LOCATION DETECTION
# ══════════════════════════════════════════════════════════════════════════════

def f_detect_location(
    state:          str,
    micro:          float,
    bos_level:      float,
    expansion_high: float,
    ext_target:     float,
    close:          float,
    macro_dir:      int,
    bars_in_state:  int,
    atr:            float,
) -> str:
    """
    Return the location label within the current state.

    PULLBACK location uses micro completion (0-100):
      <  33  → EARLY_PULLBACK
      33-66  → MID_PULLBACK
      66-82  → LATE_PULLBACK
      > 82   → PULLBACK_COMPLETE

    EXPANSION location uses progress from BOS to external target:
      < 33%  → EARLY_EXPANSION
      33-66% → MID_EXPANSION
      > 66%  → LATE_EXPANSION

    REACCUMULATION: confirmed once bars_in_state >= 5 AND low volatility.
    LIQ_SWEEP:      location set at sweep time (LOW or HIGH).
    """
    if state == STATE_PULLBACK:
        if   micro < 33:  return LOC_EARLY_PB
        elif micro < 66:  return LOC_MID_PB
        elif micro < 82:  return LOC_LATE_PB
        else:             return LOC_COMPLETE_PB

    elif state == STATE_EXPANSION:
        if np.isnan(bos_level) or np.isnan(ext_target):
            return LOC_EARLY_EXP
        span = abs(ext_target - bos_level)
        if span < atr * 0.5:
            return LOC_EARLY_EXP
        if macro_dir == 1:
            progress = (close - bos_level) / span
        else:
            progress = (bos_level - close) / span
        progress = max(0.0, min(1.0, progress))
        if   progress < 0.33: return LOC_EARLY_EXP
        elif progress < 0.66: return LOC_MID_EXP
        else:                 return LOC_LATE_EXP

    elif state == STATE_REACCUM:
        return LOC_REACCUM_CONF if bars_in_state >= 5 else LOC_MID_PB

    return LOC_UNKNOWN


# ══════════════════════════════════════════════════════════════════════════════
# 4. NARRATIVE GENERATOR
# ══════════════════════════════════════════════════════════════════════════════

def f_get_narrative(
    instrument:     str,
    state:          str,
    location:       str,
    macro_dir:      int,
    int_liq:        float,
    ext_liq:        float,
    bos_level:      float,
    close:          float,
    micro:          float,
    atr:            float,
) -> str:
    """
    Generate a plain English narrative string for the current bar.

    Example output:
      "JP225 Bullish Pullback — Late phase (74%).
       Internal liquidity at 38420 is the target.
       External liquidity at 39150 is the expansion target.
       Wait for internal sweep + fractal hold before entry."
    """
    dir_str = "Bullish" if macro_dir == 1 else "Bearish" if macro_dir == -1 else "Neutral"
    p       = lambda v: f"{v:,.2f}" if not np.isnan(v) else "n/a"
    pct     = f"{micro:.0f}%"

    # State-specific templates
    if state == STATE_EXPANSION:
        loc_map = {
            LOC_EARLY_EXP: f"Early stage — room to run toward {p(ext_liq)}.",
            LOC_MID_EXP:   f"Mid expansion — momentum should hold toward {p(ext_liq)}.",
            LOC_LATE_EXP:  f"Late stage ({pct}) — approaching {p(ext_liq)}. Avoid new longs.",
        }
        body = loc_map.get(location, "")
        hint = ("Watch for pullback to set up next entry." if location == LOC_LATE_EXP
                else "Stay with trend. No new entries mid-expansion.")
        return (f"{instrument} {dir_str} Expansion — {location.replace('_',' ').title()}. "
                f"{body} "
                f"BOS level at {p(bos_level)} is the key support. {hint}")

    elif state == STATE_PULLBACK:
        loc_map = {
            LOC_EARLY_PB:    f"Just beginning (micro {pct}). Wait — pullback not ready.",
            LOC_MID_PB:      f"In progress (micro {pct}). Internal target near {p(int_liq)}.",
            LOC_LATE_PB:     f"Late phase (micro {pct}). Internal liquidity near {p(int_liq)} is the sweep target.",
            LOC_COMPLETE_PB: f"COMPLETE (micro {pct}). Sequence should now confirm.",
        }
        body = loc_map.get(location, "")
        hint = ("Monitor for internal sweep + fractal hold + micro BOS to confirm entry."
                if location in (LOC_LATE_PB, LOC_COMPLETE_PB)
                else "Wait for deeper pullback before looking for entry.")
        return (f"{instrument} {dir_str} Pullback — {location.replace('_',' ').title()}. "
                f"{body} "
                f"External expansion target: {p(ext_liq)}. {hint}")

    elif state == STATE_REACCUM:
        return (f"{instrument} {dir_str} Reaccumulation — low-volatility compression above "
                f"BOS at {p(bos_level)}. Volume declining. Waiting for breakout. "
                f"External target: {p(ext_liq)}. No entry until BOS breaks out again.")

    elif state == STATE_DISTRIBUTION:
        return (f"{instrument} {dir_str} Distribution — price approaching {p(ext_liq)}. "
                f"RSI divergence and volume climax visible. "
                f"Do NOT add to longs. Watch for CHoCH / reversal signal.")

    elif state == STATE_LIQ_SWEEP:
        return (f"{instrument} Liquidity Sweep — fractal level violated then recovered. "
                f"Internal liquidity at {p(int_liq)} taken. "
                f"Watch for micro BOS above {p(bos_level)} to confirm entry direction.")

    elif state == STATE_RANGE:
        return (f"{instrument} Range — no structural direction. "
                f"ATR compressed. Oscillating between {p(int_liq)} and {p(ext_liq)}. "
                f"Wait for breakout with volume before entry.")

    elif state == STATE_TREND_FAIL:
        return (f"{instrument} Trend Failure — CHoCH / macro structure broken. "
                f"No entries in prior direction. Wait for new macro setup to form.")

    return f"{instrument} — structure building. State: {state}. Monitor."


# ══════════════════════════════════════════════════════════════════════════════
# 5. BOS LOCATION QUALITY MULTIPLIER
# ══════════════════════════════════════════════════════════════════════════════

def f_classify_expansion_or_pullback_bos(
    state:          str,
    location:       str,
    bos_importance: int,
) -> dict:
    """
    A BOS in LATE_PULLBACK = highest quality.
    A BOS in LATE_EXPANSION = do not enter.
    Returns quality_multiplier (0.3–1.5) applied to entry score.
    """
    base_mult = BOS_QUALITY_MULT.get(location, 0.7)

    # Importance modifier: high-importance BOS gets a small boost
    imp_adj = 0.0
    if bos_importance >= 6:    imp_adj = +0.1
    elif bos_importance >= 4:  imp_adj = +0.05
    elif bos_importance <= 1:  imp_adj = -0.1

    mult = max(0.3, min(1.5, base_mult + imp_adj))

    # Grade
    if   mult >= 1.3: grade = "PRIME"
    elif mult >= 1.0: grade = "STANDARD"
    elif mult >= 0.7: grade = "REDUCED"
    else:             grade = "AVOID"

    return {
        "quality_multiplier":  round(mult, 2),
        "grade":               grade,
        "state_at_bos":        state,
        "location_at_bos":     location,
        "bos_importance":      bos_importance,
        "reasoning": (
            f"BOS fired in {state} / {location} "
            f"(importance={bos_importance}) → "
            f"multiplier={mult:.2f} [{grade}]"
        ),
    }


# ══════════════════════════════════════════════════════════════════════════════
# 6. STATE CONFIDENCE SCORER
# ══════════════════════════════════════════════════════════════════════════════

def _state_confidence(
    state:      str,
    macro_dir:  int,
    flow:       float,
    vol_ratio:  float,
    micro:      float,
    atr:        float,
    atr_avg:    float,
    rsi_1d:     float,
    htf:        float,
) -> float:
    """
    Score 0-1 reflecting how clearly the state is expressed.
    Used to weight narratives and downstream signals.
    """
    score = 0.0

    if state == STATE_EXPANSION:
        # Strong flow + volume + HTF aligned
        score += min(1.0, max(0.0, flow) / 4.0) * 0.3
        score += min(1.0, (vol_ratio - 0.8) / 1.2) * 0.2
        score += (0.3 if htf >= 1 else 0.0) if macro_dir == 1 else (0.3 if htf <= -1 else 0.0)
        score += (0.2 if (rsi_1d > 55 and macro_dir == 1) or
                         (rsi_1d < 45 and macro_dir == -1) else 0.0)

    elif state == STATE_PULLBACK:
        # Micro > 30, flow declining, ATR moderate
        score += min(1.0, micro / 100.0) * 0.4
        score += 0.2 if atr_avg > 0 and 0.5 < (atr / atr_avg) < 1.2 else 0.0
        score += 0.2 if (flow < 0 and macro_dir == 1) or (flow > 0 and macro_dir == -1) else 0.0
        score += 0.2 if micro > 50 else 0.0

    elif state == STATE_REACCUM:
        # ATR compressed, low volume
        score += (0.5 if atr_avg > 0 and atr < 0.8 * atr_avg else 0.0)
        score += (0.3 if vol_ratio < 0.8 else 0.0)
        score += 0.2

    elif state == STATE_DISTRIBUTION:
        score += (0.4 if vol_ratio >= 1.5 else 0.2)
        score += (0.3 if micro > 70 else 0.0)
        score += 0.3

    elif state == STATE_LIQ_SWEEP:
        score += (0.5 if vol_ratio >= 1.5 else 0.3)
        score += 0.5

    elif state == STATE_RANGE:
        score += (0.6 if atr_avg > 0 and atr < 0.75 * atr_avg else 0.2)
        score += 0.4

    elif state == STATE_TREND_FAIL:
        score = 0.7

    return round(min(1.0, max(0.0, score)), 3)


# ══════════════════════════════════════════════════════════════════════════════
# 7. BAR-BY-BAR MACRO STATE MACHINE
# ══════════════════════════════════════════════════════════════════════════════

def run_macro_state_machine(
    df:         pd.DataFrame,
    instrument: str,
    tf:         str,
    verbose:    bool = False,
) -> list[dict]:
    """
    Primary entry point.  Run bar-by-bar structural state detection.

    Returns a list of dicts — one per bar (from bar 50 onward).
    Each dict contains: bar index, datetime, state, location,
    macro_dir, bos_level, expansion_high, ext_target, bars_in_state,
    flow, micro, vol_ratio, confidence, bos_fired, bos_quality,
    int_liq, narrative.

    State transition map:
      UNKNOWN     → EXPANSION   on BOS + macro direction
                  → RANGE       on ATR compression without BOS
      EXPANSION   → PULLBACK    on price retreat (> 0.3 ATR below 5-bar high)
                  → DISTRIBUTION near ext target + vol climax + RSI divergence
                  → LIQ_SWEEP   on sweep candle
                  → TREND_FAIL  on macro direction reversal
      PULLBACK    → EXPANSION   on new BOS
                  → REACCUM     on ATR compression + no new lows
                  → TREND_FAIL  on close below BOS - 0.5 ATR
                  → LIQ_SWEEP   on sweep candle at pullback low
      REACCUM     → EXPANSION   on new BOS
                  → TREND_FAIL  on close below BOS - 0.5 ATR
      DISTRIBUTION→ TREND_FAIL  on price breakdown
                  → EXPANSION   on failed distribution
      LIQ_SWEEP   → resolves to PULLBACK or EXPANSION after 3 bars
      RANGE       → EXPANSION   on volume breakout
      TREND_FAIL  → UNKNOWN     then re-establishes with new direction
    """
    n      = len(df)
    closes = df["close"].values
    highs  = df["high"].values
    lows   = df["low"].values
    opens  = df["open"].values
    atrs   = df["atr14"].values
    volr   = df["vol_ratio"].values
    rsi_b  = df["rsi_base"].values
    rsi1d  = df["rsi_1d"].values
    htf    = df["htf_align"].values
    flow   = df["flow_score"].values
    micro_ = df["micro"].values
    mini_  = df["mini"].values
    lph    = df["last_pivot_high"].values
    lpl    = df["last_pivot_low"].values
    pp_hi  = df["prior_pivot_high"].values
    pp_lo  = df["prior_pivot_low"].values
    sess   = df["session"].values

    # Pre-computed rolling statistics
    atr_avg20 = _rolling_mean(atrs, 20)
    hi5       = _rolling_max(highs, 5)    # 5-bar rolling high
    lo5       = _rolling_min(lows,  5)    # 5-bar rolling low
    # Shift hi5/lo5 by 1 to avoid lookahead (we see completed bars)
    hi5_prev  = np.roll(hi5, 1); hi5_prev[0] = np.nan
    lo5_prev  = np.roll(lo5, 1); lo5_prev[0] = np.nan

    # Rolling external target: 50-bar high / low as HTF liquidity proxy
    ext_hi50 = _rolling_max(highs, 50)
    ext_lo50 = _rolling_min(lows,  50)
    ext_hi50 = np.roll(ext_hi50, 1); ext_hi50[0] = np.nan
    ext_lo50 = np.roll(ext_lo50, 1); ext_lo50[0] = np.nan

    # ── State machine variables ──────────────────────────────────────────
    state          = STATE_UNKNOWN
    macro_dir      = 0             # +1 bull / -1 bear / 0 neutral
    bos_level      = np.nan        # most-recent BOS level (micro — updates each BOS)
    struct_bos     = np.nan        # STRUCTURAL BOS — only updates from non-EXPANSION
                                   # context.  Used for TREND_FAILURE gate (wider).
    expansion_high = np.nan        # peak reached during expansion
    ext_target     = np.nan        # external liquidity target
    int_liq        = np.nan        # internal liquidity (pullback target)
    bars_in_state  = 0
    state_start    = 50
    flow_peak      = 0.0           # peak flow score in current expansion
    no_new_low_ctr = 0             # counter for reaccum detection
    sweep_bar      = -10           # last sweep bar
    pre_sweep_state = STATE_UNKNOWN  # state before LIQ_SWEEP

    results = []

    for i in range(50, n):
        cl   = _safe(closes, i, np.nan)
        hi   = _safe(highs,  i, np.nan)
        lo   = _safe(lows,   i, np.nan)
        op   = _safe(opens,  i, np.nan)
        if np.isnan(cl): continue

        atr  = _safe(atrs,   i, 1.0);   atr  = max(atr,  1e-6)
        aavg = _safe(atr_avg20, i, atr); aavg = max(aavg, 1e-6)
        vr   = _safe(volr,   i, 1.0)
        r1   = _safe(rsi1d,  i, 50.0)
        rb   = _safe(rsi_b,  i, 50.0)
        h    = _safe(htf,    i, 0.0)
        fl   = _safe(flow,   i, 0.0)
        mic  = _safe(micro_, i, 0.0)
        mn   = _safe(mini_,  i, 50.0)
        ph   = _safe(lph,    i, np.nan)
        pl   = _safe(lpl,    i, np.nan)
        pph  = _safe(pp_hi,  i, np.nan)
        ppl  = _safe(pp_lo,  i, np.nan)

        hi5p = _safe(hi5_prev, i, np.nan)
        lo5p = _safe(lo5_prev, i, np.nan)
        cl_prev = _safe(closes, i - 1, cl)

        candle_range = max(hi - lo, atr * 0.1)
        lower_wick   = max(min(op, cl) - lo, 0)
        upper_wick   = max(hi - max(op, cl), 0)

        # ── Macro direction update ───────────────────────────────────────
        prev_macro = macro_dir
        if macro_dir == 0:
            if r1 > 52 and h >= 1:  macro_dir = +1
            elif r1 < 48 and h <= -1: macro_dir = -1
        elif macro_dir == +1:
            if r1 < 42 and h <= -1:
                macro_dir = -1
                bos_level = np.nan
                struct_bos = np.nan
                expansion_high = np.nan
        elif macro_dir == -1:
            if r1 > 58 and h >= 1:
                macro_dir = +1
                bos_level = np.nan
                struct_bos = np.nan
                expansion_high = np.nan

        # ── BOS detection ───────────────────────────────────────────────
        bos_fired  = False
        bos_dir    = 0
        bos_level_new = bos_level

        if macro_dir == +1 and not np.isnan(ph):
            if cl > ph and cl_prev <= ph:
                bos_fired = True; bos_dir = +1
                bos_level_new = ph
                # External target: prior pivot high > BOS, else 50-bar high
                if not np.isnan(pph) and pph > ph:
                    ext_target = pph
                elif not np.isnan(ext_hi50[i]) and ext_hi50[i] > ph:
                    ext_target = ext_hi50[i]
                else:
                    ext_target = ph + atr * 3.0   # fallback
                int_liq = pl if not np.isnan(pl) else ph - atr

        elif macro_dir == -1 and not np.isnan(pl):
            if cl < pl and cl_prev >= pl:
                bos_fired = True; bos_dir = -1
                bos_level_new = pl
                if not np.isnan(ppl) and ppl < pl:
                    ext_target = ppl
                elif not np.isnan(ext_lo50[i]) and ext_lo50[i] < pl:
                    ext_target = ext_lo50[i]
                else:
                    ext_target = pl - atr * 3.0
                int_liq = ph if not np.isnan(ph) else pl + atr

        if bos_fired:
            bos_level = bos_level_new
            # struct_bos only updates when the BOS fires from outside EXPANSION
            # (i.e., from pullback / reaccum / range / unknown / trend_fail / sweep).
            # This gives the structural reference level for TREND_FAILURE.
            if state not in (STATE_EXPANSION,):
                struct_bos = bos_level_new

        # ── Track expansion extreme ──────────────────────────────────────
        if state == STATE_EXPANSION:
            if macro_dir == +1:
                if np.isnan(expansion_high) or hi > expansion_high:
                    expansion_high = hi
            elif macro_dir == -1:
                if np.isnan(expansion_high) or lo < expansion_high:
                    expansion_high = lo
        elif bos_fired:
            expansion_high = hi if macro_dir == +1 else lo

        # ── Update internal liquidity continuously ───────────────────────
        # int_liq = nearest fractal in pullback direction
        if macro_dir == +1 and not np.isnan(pl):
            if np.isnan(int_liq) or pl > int_liq:  # nearest low above old int_liq
                int_liq = pl
        elif macro_dir == -1 and not np.isnan(ph):
            if np.isnan(int_liq) or ph < int_liq:
                int_liq = ph

        # ── Update flow peak ─────────────────────────────────────────────
        if macro_dir == +1 and fl > flow_peak:
            flow_peak = fl
        elif macro_dir == -1 and fl < flow_peak:
            flow_peak = fl

        # ── LIQ_SWEEP detection ──────────────────────────────────────────
        # Sweep = candle breaks fractal level with wick, then recovers.
        # Volume spike required for instruments with volume data.
        # Price-action proxy (wick >= 60% of range) used for VOLUME_UNAVAILABLE.
        is_sweep   = False
        sweep_dir  = 0
        has_vol    = f_has_volume(instrument)
        vol_thresh = 1.3 if has_vol else 0.0   # no vol req for cash indices
        wick_min   = 0.45 if has_vol else 0.60  # stricter wick for no-vol
        if macro_dir == +1 and not np.isnan(pl):
            wick_below = lower_wick / candle_range
            vol_ok     = (vr >= vol_thresh) if has_vol else True
            if lo < pl and cl > pl and wick_below >= wick_min and vol_ok:
                is_sweep = True; sweep_dir = +1
        elif macro_dir == -1 and not np.isnan(ph):
            wick_above = upper_wick / candle_range
            vol_ok     = (vr >= vol_thresh) if has_vol else True
            if hi > ph and cl < ph and wick_above >= wick_min and vol_ok:
                is_sweep = True; sweep_dir = -1

        # ── State machine transitions ────────────────────────────────────
        prev_state = state

        if macro_dir == 0:
            state = STATE_UNKNOWN

        elif is_sweep:
            pre_sweep_state = state
            state           = STATE_LIQ_SWEEP
            sweep_bar       = i

        elif bos_fired:
            state      = STATE_EXPANSION
            flow_peak  = fl
            no_new_low_ctr = 0

        elif state in (STATE_UNKNOWN, STATE_TREND_FAIL, STATE_RANGE):
            # TREND_FAILURE → RANGE when ATR compresses (market digesting the failure)
            # This prevents getting permanently stuck in TREND_FAILURE.
            if aavg > 0 and atr <= 0.85 * aavg:
                state = STATE_RANGE
            elif state == STATE_RANGE and aavg > 0 and atr > 0.9 * aavg and abs(fl) >= 2:
                # Range expanding with directional flow → go back to UNKNOWN for next BOS
                state = STATE_UNKNOWN

        elif state == STATE_EXPANSION:
            if macro_dir == +1:
                # Trend failure — uses STRUCTURAL BOS level (1.5 ATR grace)
                # struct_bos is only set when BOS fired from non-expansion context.
                # This prevents micro pivot re-anchoring from causing false failures.
                fail_level = (struct_bos - 1.5 * atr
                              if not np.isnan(struct_bos)
                              else bos_level - 1.5 * atr)
                if not np.isnan(fail_level) and cl < fail_level:
                    state = STATE_TREND_FAIL
                # Distribution: near ext target + vol climax + RSI divergence
                elif (not np.isnan(ext_target) and
                      not np.isnan(bos_level) and
                      ext_target > bos_level and
                      abs(ext_target - bos_level) > atr * 0.5 and
                      bars_in_state >= 3):
                    prog = (cl - bos_level) / (ext_target - bos_level)
                    div  = _rsi_divergence(closes, rsi_b, i, lookback=8, direction=+1)
                    if prog >= 0.75 and _volume_climax(volr, i) and div:
                        state = STATE_DISTRIBUTION
                # Pullback: price retreated from recent high + flow declining
                # No micro requirement — micro lags price action by 2-5 bars
                elif (not np.isnan(hi5p) and
                      cl < hi5p - 0.3 * atr and
                      bars_in_state >= 3 and
                      (fl < flow_peak - 1 or fl < 0)):
                    state = STATE_PULLBACK
                    flow_peak = fl
                    no_new_low_ctr = 0
            else:  # bear expansion
                fail_level = (struct_bos + 1.5 * atr
                              if not np.isnan(struct_bos)
                              else bos_level + 1.5 * atr)
                if not np.isnan(fail_level) and cl > fail_level:
                    state = STATE_TREND_FAIL
                elif (not np.isnan(ext_target) and
                      not np.isnan(bos_level) and
                      ext_target < bos_level and
                      abs(bos_level - ext_target) > atr * 0.5 and
                      bars_in_state >= 3):
                    prog = (bos_level - cl) / (bos_level - ext_target)
                    div  = _rsi_divergence(closes, rsi_b, i, lookback=8, direction=-1)
                    if prog >= 0.75 and _volume_climax(volr, i) and div:
                        state = STATE_DISTRIBUTION
                elif (not np.isnan(lo5p) and
                      cl > lo5p + 0.3 * atr and
                      bars_in_state >= 3 and
                      (fl > flow_peak + 1 or fl > 0)):
                    state = STATE_PULLBACK
                    flow_peak = fl
                    no_new_low_ctr = 0

        elif state == STATE_PULLBACK:
            if macro_dir == +1:
                # Trend failure: pullback breaks structural BOS (2.0 ATR margin)
                fail_level = (struct_bos - 2.0 * atr
                              if not np.isnan(struct_bos)
                              else bos_level - 2.0 * atr)
                if not np.isnan(fail_level) and cl < fail_level:
                    state = STATE_TREND_FAIL
                # Reaccumulation: ATR compressed + no new lows for 5 bars.
                # Volume dry-up check only for instruments with volume data.
                else:
                    vol_dry = (vr < 0.9) if has_vol else True
                    if aavg > 0 and atr <= 0.85 * aavg and vol_dry:
                        window_lows = lows[max(0, i - 10): i]
                        if len(window_lows) >= 3 and lo >= float(np.min(window_lows)) - atr * 0.1:
                            no_new_low_ctr += 1
                        else:
                            no_new_low_ctr = 0
                        if no_new_low_ctr >= 5:
                            state = STATE_REACCUM
                    else:
                        no_new_low_ctr = 0
            else:  # bear pullback
                fail_level = (struct_bos + 2.0 * atr
                              if not np.isnan(struct_bos)
                              else bos_level + 2.0 * atr)
                if not np.isnan(fail_level) and cl > fail_level:
                    state = STATE_TREND_FAIL
                else:
                    vol_dry = (vr < 0.9) if has_vol else True
                    if aavg > 0 and atr <= 0.85 * aavg and vol_dry:
                        window_highs = highs[max(0, i - 10): i]
                        if len(window_highs) >= 3 and hi <= float(np.max(window_highs)) + atr * 0.1:
                            no_new_low_ctr += 1
                        else:
                            no_new_low_ctr = 0
                        if no_new_low_ctr >= 5:
                            state = STATE_REACCUM
                    else:
                        no_new_low_ctr = 0

        elif state == STATE_REACCUM:
            if macro_dir == +1:
                fail_level = (struct_bos - 2.0 * atr
                              if not np.isnan(struct_bos)
                              else bos_level - 2.0 * atr)
                if not np.isnan(fail_level) and cl < fail_level:
                    state = STATE_TREND_FAIL
                # bos_fired handled above — would set EXPANSION
            else:
                fail_level = (struct_bos + 2.0 * atr
                              if not np.isnan(struct_bos)
                              else bos_level + 2.0 * atr)
                if not np.isnan(fail_level) and cl > fail_level:
                    state = STATE_TREND_FAIL

        elif state == STATE_DISTRIBUTION:
            # Distribution fails (price resumes) or confirms (price reverses)
            if macro_dir == +1 and not np.isnan(bos_level):
                if cl < bos_level - 0.3 * atr:
                    state = STATE_TREND_FAIL
                elif cl > _safe(ext_hi50, i, cl) and vr < 1.0:
                    state = STATE_EXPANSION   # false distribution
            elif macro_dir == -1 and not np.isnan(bos_level):
                if cl > bos_level + 0.3 * atr:
                    state = STATE_TREND_FAIL

        elif state == STATE_LIQ_SWEEP:
            # Resolve after 3 bars: go to EXPANSION or PULLBACK based on direction
            bars_since_sweep = i - sweep_bar
            if bars_since_sweep >= 3:
                if sweep_dir == +1:
                    # Sweep of lows in bull context → PULLBACK likely complete
                    state = STATE_PULLBACK if pre_sweep_state != STATE_EXPANSION else STATE_EXPANSION
                else:
                    state = STATE_PULLBACK if pre_sweep_state != STATE_EXPANSION else STATE_EXPANSION

        # ── Update bars_in_state counter ─────────────────────────────────
        if state == prev_state:
            bars_in_state += 1
        else:
            bars_in_state = 1
            state_start   = i
            # Reset flow peak on state entry
            if state in (STATE_EXPANSION, STATE_PULLBACK):
                flow_peak = fl
            if state in (STATE_PULLBACK, STATE_EXPANSION, STATE_REACCUM):
                no_new_low_ctr = 0

        # ── Location within state ─────────────────────────────────────────
        location = f_detect_location(
            state         = state,
            micro         = mic,
            bos_level     = bos_level,
            expansion_high= expansion_high,
            ext_target    = ext_target,
            close         = cl,
            macro_dir     = macro_dir,
            bars_in_state = bars_in_state,
            atr           = atr,
        )

        # LIQ_SWEEP location override
        if state == STATE_LIQ_SWEEP:
            location = LOC_SWEEP_LOW if sweep_dir == +1 else LOC_SWEEP_HIGH

        # ── BOS quality if BOS fired ──────────────────────────────────────
        bos_quality_dict = None
        if bos_fired:
            # Use the PREVIOUS bar's location (where the BOS was born)
            prev_loc = results[-1]["location"] if results else LOC_UNKNOWN
            bos_quality_dict = f_classify_expansion_or_pullback_bos(
                state         = prev_state,
                location      = prev_loc,
                bos_importance= _compute_bos_importance(fl, vr, h, rb, mic),
            )

        # ── Confidence ────────────────────────────────────────────────────
        conf = _state_confidence(state, macro_dir, fl, vr, mic, atr, aavg, r1, h)

        # ── Narrative ─────────────────────────────────────────────────────
        narrative = f_get_narrative(
            instrument = instrument,
            state      = state,
            location   = location,
            macro_dir  = macro_dir,
            int_liq    = int_liq,
            ext_liq    = ext_target,
            bos_level  = bos_level,
            close      = cl,
            micro      = mic,
            atr        = atr,
        )

        results.append({
            "bar":            i,
            "datetime_utc":   str(df["datetime_utc"].iloc[i]),
            "close":          round(cl, 5),
            "state":          state,
            "macro_dir":      macro_dir,
            "location":       location,
            "bos_level":      round(bos_level, 5) if not np.isnan(bos_level) else None,
            "struct_bos":     round(struct_bos, 5) if not np.isnan(struct_bos) else None,
            "expansion_high": round(expansion_high, 5) if not np.isnan(expansion_high) else None,
            "ext_target":     round(ext_target, 5) if not np.isnan(ext_target) else None,
            "int_liq":        round(int_liq, 5) if not np.isnan(int_liq) else None,
            "bars_in_state":  bars_in_state,
            "flow":           fl,
            "micro":          round(mic, 1),
            "vol_ratio":      round(vr, 3),
            "atr":            round(atr, 5),
            "confidence":     conf,
            "bos_fired":      bos_fired,
            "bos_quality":    bos_quality_dict,
            "session":        sess[i],
            "narrative":      narrative,
        })

        if verbose and bos_fired:
            print(f"  bar={i}  BOS {'+1' if bos_dir == 1 else '-1'}  "
                  f"state={state}  loc={location}  "
                  f"quality={bos_quality_dict['grade'] if bos_quality_dict else '?'}")

    return results


# ══════════════════════════════════════════════════════════════════════════════
# 8. CONVENIENCE: PER-BAR STATE (for live monitor integration)
# ══════════════════════════════════════════════════════════════════════════════

def f_detect_macro_state(
    df:         pd.DataFrame,
    instrument: str,
    tf:         str,
) -> dict:
    """
    Convenience wrapper — returns the CURRENT (last bar) state dict.
    Also returns state_changed bool (True if state differs from previous bar).
    Called from live/golden_highway_monitor.py every 5 minutes.
    """
    results = run_macro_state_machine(df, instrument, tf)
    if not results:
        return {"state": STATE_UNKNOWN, "confidence": 0.0,
                "bars_in_state": 0, "state_changed": False,
                "location": LOC_UNKNOWN, "narrative": "No data."}
    curr = results[-1]
    prev = results[-2] if len(results) >= 2 else curr
    curr["state_changed"] = (curr["state"] != prev["state"])
    return curr


# ══════════════════════════════════════════════════════════════════════════════
# 9. BOS IMPORTANCE (inline, no external dependency)
# ══════════════════════════════════════════════════════════════════════════════

def _compute_bos_importance(
    flow:    float,
    vol_r:   float,
    htf:     float,
    rsi_1h:  float,
    micro:   float,
) -> int:
    """
    Compute BOS importance score 0-7 from available columns.
    This is the column-99 equivalent built from feature data.

    Pillar mapping (simplified for macro_structure_engine):
      P1: volume confirms (vol_r >= 1.3)
      P2: flow aligned (flow >= 2 bull / <= -2 bear)
      P3: HTF aligned (htf >= 1 bull / <= -1 bear)
      P4: RSI in range (rsi_1h 45-75 bull / 25-55 bear)
      P5: micro completion (micro >= 60)
      P6: flow peak met (flow >= 3)
      P7: macro RSI (inferred from caller context, +0 default here)
    """
    score = 0
    score += 1 if vol_r >= 1.3 else 0
    score += 1 if flow >= 2 else 0
    score += 1 if htf >= 1 else 0
    score += 1 if 45 <= rsi_1h <= 75 else 0
    score += 1 if micro >= 60 else 0
    score += 1 if flow >= 3 else 0
    score += 1 if vol_r >= 1.8 else 0
    return score


# ══════════════════════════════════════════════════════════════════════════════
# 10. DIAGNOSTIC — 10 SAMPLE CLASSIFICATIONS
# ══════════════════════════════════════════════════════════════════════════════

def print_sample_classifications(
    instrument: str = "XAUUSD",
    tf:         str = "1H",
    n_samples:  int = 10,
) -> None:
    """
    Print 10 representative state classifications with full narrative.
    Used for Task 1 validation.
    """
    print(f"\n{'═'*80}")
    print(f"  MACRO STATE ENGINE — {n_samples} SAMPLE CLASSIFICATIONS")
    print(f"  Instrument: {instrument}  TF: {tf}")
    print(f"{'═'*80}")

    df = build_features(instrument, tf)
    if df is None:
        print("  ERROR: No data"); return

    results = run_macro_state_machine(df, instrument, tf)
    total   = len(results)
    print(f"  Total bars classified: {total}")

    # Collect one example per state (most recent occurrence)
    seen_states: dict[str, dict] = {}
    for r in results:
        seen_states[r["state"]] = r   # overwrite → keeps most recent

    # Also add most recent bar regardless of state
    samples = list(seen_states.values())
    last    = results[-1]
    if last not in samples:
        samples.append(last)

    # Sort by bar index
    samples.sort(key=lambda x: x["bar"])

    # If fewer than n_samples, supplement with evenly-spaced bars
    if len(samples) < n_samples:
        step = max(1, total // n_samples)
        extra = [results[j] for j in range(0, total, step)]
        existing_bars = {s["bar"] for s in samples}
        for e in extra:
            if e["bar"] not in existing_bars and len(samples) < n_samples:
                samples.append(e)
                existing_bars.add(e["bar"])
        samples.sort(key=lambda x: x["bar"])

    samples = samples[:n_samples]

    # ── State distribution summary ────────────────────────────────────────
    from collections import Counter
    state_counts = Counter(r["state"] for r in results)
    print(f"\n  STATE DISTRIBUTION (all {total} bars):")
    for st, cnt in state_counts.most_common():
        pct = cnt / total * 100
        print(f"    {st:<22} {cnt:>5} bars  ({pct:>5.1f}%)")

    # ── Sample classifications ────────────────────────────────────────────
    print(f"\n  {'─'*78}")
    print(f"  SAMPLE CLASSIFICATIONS ({len(samples)} bars shown)")
    print(f"  {'─'*78}")

    for idx, r in enumerate(samples, 1):
        bq_str = ""
        if r["bos_fired"] and r["bos_quality"]:
            bq = r["bos_quality"]
            bq_str = (f"\n  ┌─ BOS QUALITY: {bq['grade']} "
                      f"(mult={bq['quality_multiplier']}) — "
                      f"{bq['reasoning']}")

        print(f"\n  [{idx:>2}] Bar {r['bar']:>5}  "
              f"{r['datetime_utc'][:16]}  "
              f"Close: {r['close']:>10,.2f}  "
              f"Session: {r['session']}")
        print(f"       STATE:    {r['state']:<22}  Location: {r['location']}")
        print(f"       Dir: {'BULL' if r['macro_dir']==1 else 'BEAR' if r['macro_dir']==-1 else 'NONE':4}  "
              f"Flow: {r['flow']:>+5.1f}  "
              f"Micro: {r['micro']:>5.1f}%  "
              f"VolR: {r['vol_ratio']:>5.2f}x  "
              f"BosInState: {r['bars_in_state']:>4}  "
              f"Confidence: {r['confidence']:.2f}")
        print(f"       BOS_level: {str(r['bos_level']):>12}  "
              f"Int_liq: {str(r['int_liq']):>12}  "
              f"Ext_target: {str(r['ext_target']):>12}")
        if bq_str: print(bq_str)
        # Wrap narrative
        narr = r["narrative"]
        words = narr.split()
        line  = "  │  "
        for w in words:
            if len(line) + len(w) + 1 > 78:
                print(line)
                line = "  │  " + w + " "
            else:
                line += w + " "
        if line.strip(): print(line)
        print(f"  {'─'*78}")

    print(f"\n  CURRENT STATE (latest bar):")
    last = results[-1]
    print(f"  {last['datetime_utc'][:16]}  "
          f"STATE={last['state']}  "
          f"LOCATION={last['location']}  "
          f"Confidence={last['confidence']:.2f}")
    print(f"  {last['narrative']}")
    print()


# ══════════════════════════════════════════════════════════════════════════════
# 11. MAIN
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print_sample_classifications("XAUUSD", "1H", n_samples=10)
    print()
    # Also show one other instrument for comparison
    print_sample_classifications("UK100",  "1H", n_samples=5)
    print_sample_classifications("JP225",  "1H", n_samples=5)
