"""
research/structure_engine.py
============================
Golden Highway — Per-Instrument Structural Calibration Engine.

Single source of truth for:
  1. Fractal lookback parameters (left / right / min_age / min_swing_atr)
  2. BOS validity rules (5 rules — close-based, age, swing size, body, no-reversal)
  3. BOS importance scoring (7 pillars — replaces original engine version)
  4. Pullback zone measurement (Fibonacci depth zones + invalidation logic)
  5. Pullback end score evaluation (5 conditions per instrument)
  6. Entry rules per instrument (min_end_score, min_bos_score, sl_mult, sessions)

Used by:
  - research/golden_highway_engine.py  (backtest)
  - live/golden_highway_monitor.py     (live loop)
  - pine/golden_highway.pine           (Pine Script mirror of these values)

All thresholds sourced from H2 JTE research across IC Markets instruments.
Do NOT override individual values in caller code — update the tables here.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# ══════════════════════════════════════════════════════════════════════════════
# 1. FRACTAL CALIBRATION TABLE
#    (instrument, timeframe) -> (left_bars, right_bars, min_age, min_swing_atr)
#
#    left_bars     : bars to the left of pivot (Williams lookback)
#    right_bars    : bars to the right (2-3 always — timeliness)
#    min_age       : minimum bars since pivot confirmation before it can be BOS'd
#    min_swing_atr : minimum swing height as ATR(14) multiple
# ══════════════════════════════════════════════════════════════════════════════

# Structure: { instrument: { timeframe: (left, right, min_age, min_swing_atr) } }
_FRACTAL_TABLE: dict[str, dict[str, tuple[int, int, int, float]]] = {
    "JP225": {
        "1H":  (8,  3, 5, 1.0),
        "4H":  (10, 3, 5, 1.0),
        "15m": (5,  2, 3, 0.8),
        "5m":  (3,  2, 2, 0.6),
    },
    "DE40": {
        "1H":  (8,  3, 5, 0.8),
        "4H":  (10, 3, 5, 0.8),
        "15m": (5,  2, 3, 0.6),
        "5m":  (3,  2, 2, 0.5),
    },
    "UK100": {
        "1H":  (7,  3, 5, 0.8),
        "4H":  (10, 3, 5, 0.8),
        "15m": (4,  2, 3, 0.6),
        "5m":  (3,  2, 2, 0.5),
    },
    "HK50": {
        "1H":  (7,  3, 4, 0.8),
        "4H":  (8,  3, 5, 0.8),
        "15m": (4,  2, 3, 0.6),
        "5m":  (3,  2, 2, 0.5),
    },
    "US30": {
        "1H":  (7,  2, 3, 0.8),
        "4H":  (10, 3, 5, 0.8),
        "15m": (4,  2, 3, 0.6),
        "5m":  (2,  2, 2, 0.5),
    },
    "USTEC": {
        "1H":  (7,  2, 3, 0.8),
        "4H":  (10, 3, 5, 0.8),
        "15m": (3,  2, 3, 0.6),
        "5m":  (2,  2, 2, 0.5),
    },
    "SPX500": {
        "1H":  (7,  2, 3, 0.8),
        "4H":  (10, 3, 5, 0.8),
        "15m": (4,  2, 3, 0.6),
        "5m":  (3,  2, 2, 0.5),
    },
    "XAUUSD": {
        "1H":  (8,  3, 4, 1.0),
        "4H":  (10, 3, 5, 1.0),
        "15m": (5,  2, 3, 0.8),
        "5m":  (4,  2, 2, 0.6),
    },
    "XAGUSD": {
        "1H":  (7,  3, 3, 0.8),
        "4H":  (10, 3, 5, 0.8),
        "15m": (5,  3, 3, 0.7),
        "5m":  (3,  2, 2, 0.5),
    },
    "GBPUSD": {
        "1H":  (7,  3, 4, 0.6),
        "4H":  (8,  3, 4, 0.6),
        "15m": (5,  2, 3, 0.5),
        "5m":  (3,  2, 2, 0.4),
    },
    "EURUSD": {
        "1H":  (7,  3, 4, 0.6),
        "4H":  (8,  3, 4, 0.6),
        "15m": (5,  2, 3, 0.5),
        "5m":  (3,  2, 2, 0.4),
    },
    "GBPJPY": {
        "1H":  (7,  3, 3, 0.6),
        "4H":  (10, 3, 4, 0.6),
        "15m": (5,  3, 3, 0.5),
        "5m":  (3,  2, 2, 0.4),
    },
    "EURJPY": {
        "1H":  (7,  3, 4, 0.6),
        "4H":  (8,  3, 4, 0.6),
        "15m": (5,  2, 3, 0.5),
        "5m":  (3,  2, 2, 0.4),
    },
    "USDJPY": {
        "1H":  (7,  3, 4, 0.6),
        "4H":  (8,  3, 4, 0.6),
        "15m": (4,  2, 3, 0.5),
        "5m":  (3,  2, 2, 0.4),
    },
    "AUDUSD": {
        "1H":  (7,  3, 4, 0.6),
        "4H":  (8,  3, 4, 0.6),
        "15m": (5,  2, 3, 0.5),
        "5m":  (3,  2, 2, 0.4),
    },
    "USDCAD": {
        "1H":  (7,  3, 4, 0.6),
        "4H":  (8,  3, 4, 0.6),
        "15m": (5,  2, 3, 0.5),
        "5m":  (3,  2, 2, 0.4),
    },
    "BTCUSD": {
        "1H":  (6,  2, 3, 0.8),
        "4H":  (8,  3, 4, 0.8),
        "15m": (4,  2, 3, 0.6),
        "5m":  (3,  2, 2, 0.5),
    },
    "ETHUSD": {
        "1H":  (6,  2, 3, 0.8),
        "4H":  (8,  3, 4, 0.8),
        "15m": (4,  2, 3, 0.6),
        "5m":  (3,  2, 2, 0.5),
    },
}

# Default for any unlisted instrument
_FRACTAL_DEFAULT: dict[str, tuple[int, int, int, float]] = {
    "1H":  (7, 3, 4, 0.8),
    "4H":  (8, 3, 5, 0.8),
    "15m": (5, 2, 3, 0.6),
    "5m":  (3, 2, 2, 0.5),
    "D":   (5, 3, 5, 1.0),
}


def f_get_fractal_params(instrument: str, timeframe: str) -> tuple[int, int, int, float]:
    """
    Returns (left_bars, right_bars, min_age_bars, min_swing_atr_mult).

    left_bars        : pivot detection lookback (left side)
    right_bars       : pivot detection confirmation (right side)
    min_age_bars     : minimum bars since pivot high/low before it can be BOS'd
    min_swing_atr    : minimum swing size as ATR(14) multiple (Rule 3)

    Examples:
        left, right, min_age, min_swing = f_get_fractal_params("XAUUSD", "4H")
        # -> (10, 3, 5, 1.0)

        left, right, min_age, min_swing = f_get_fractal_params("GBPUSD", "1H")
        # -> (7, 3, 4, 0.6)
    """
    inst_table = _FRACTAL_TABLE.get(instrument.upper(), {})
    tf_norm    = _normalise_tf(timeframe)
    result     = inst_table.get(tf_norm)
    if result is None:
        result = _FRACTAL_DEFAULT.get(tf_norm, (7, 3, 4, 0.8))
    return result


# ══════════════════════════════════════════════════════════════════════════════
# 2. BOS VALIDITY RULES + 7-PILLAR IMPORTANCE SCORING
# ══════════════════════════════════════════════════════════════════════════════

# BOS Importance grade thresholds (new scale from H2 JTE research)
BOS_GRADE_AP  = 6   # 6-7 = A+ (full size)
BOS_GRADE_A   = 5   # 5   = A  (standard size)
BOS_GRADE_B   = 4   # 4   = B  (take with confirmation)
BOS_GRADE_C   = 3   # 3   = C  (wait for extra confirmation)
BOS_GRADE_SKIP = 2  # 0-2 = SKIP

# Body quality minimum: BOS candle body must be >= this fraction of the full range
BOS_BODY_MIN_FRAC = 0.30

# Minimum swing size by instrument (ATR multiples) — from Rule 3
_SWING_ATR_MIN: dict[str, float] = {
    "JP225":  1.0,
    "DE40":   0.8,
    "UK100":  0.8,
    "HK50":   0.8,
    "US30":   0.8,
    "USTEC":  0.8,
    "SPX500": 0.8,
    "XAUUSD": 1.0,
    "XAGUSD": 0.8,
    "GBPUSD": 0.6,
    "EURUSD": 0.6,
    "GBPJPY": 0.6,
    "EURJPY": 0.6,
    "USDJPY": 0.6,
    "AUDUSD": 0.6,
    "USDCAD": 0.6,
    "BTCUSD": 0.8,
    "ETHUSD": 0.8,
}

# Minimum fractal age (bars) — from Rule 2
_MIN_FRACTAL_AGE: dict[str, int] = {
    "JP225":  5,
    "DE40":   5,
    "UK100":  5,
    "HK50":   4,
    "US30":   3,
    "USTEC":  3,
    "SPX500": 3,
    "XAUUSD": 4,
    "XAGUSD": 3,
    "GBPUSD": 4,
    "EURUSD": 4,
    "GBPJPY": 3,
    "EURJPY": 4,
    "USDJPY": 4,
    "AUDUSD": 4,
    "USDCAD": 4,
    "BTCUSD": 3,
    "ETHUSD": 3,
}


def f_classify_bos(
    instrument: str,
    timeframe: str,
    bar: dict,
) -> dict:
    """
    Evaluate BOS quality for a single bar.

    bar dict keys expected:
      direction        : +1 (bull BOS) or -1 (bear BOS)
      close            : current bar close
      open             : current bar open
      high             : current bar high
      low              : current bar low
      close_prev       : previous bar close
      fractal_level    : fractal high (bull) or low (bear) being broken
      fractal_bar_idx  : bar index when the fractal was detected
      current_bar_idx  : current bar index
      swing_origin     : swing low (bull) or swing high (bear) before this BOS
                         (used for swing size calculation)
      atr14            : ATR(14) at current bar
      rsi_1h           : RSI(14) on entry TF
      rsi_1d           : RSI(14) on Daily TF
      micro            : Micro Completion (0-100)
      mini             : Mini Composite RSI (0-100)
      flow_score       : Flow Score (-5 to +5)
      ema50_4h         : 4H 50-period EMA value
      prior_pivot_high : most recent prior fractal high (before current) for sweep check (bull)
      prior_pivot_low  : most recent prior fractal low (before current) for sweep check (bear)
      session          : one of TOKYO / LONDON / NY / OVERLAP / OFFHOURS

    Returns dict:
      valid       : True if all 5 validity rules pass
      importance  : int 0-7 (sum of 7 pillars)
      grade       : 'A+' / 'A' / 'B' / 'C' / 'SKIP'
      bos_type    : 'A' / 'B' / 'C'  (for engine compatibility)
      rules       : dict of which Rule 1-5 passed (True/False)
      pillars     : dict of which Pillar 1-7 passed (True/False)
      reason      : short string explaining any failure
    """
    d   = bar.get("direction", 1)
    cl  = float(bar.get("close",            0))
    op  = float(bar.get("open",             0))
    hi  = float(bar.get("high",             0))
    lo  = float(bar.get("low",              0))
    cl0 = float(bar.get("close_prev",       cl))
    fl  = float(bar.get("fractal_level",    0))
    f_idx    = int(bar.get("fractal_bar_idx",   0))
    cur_idx  = int(bar.get("current_bar_idx",   f_idx + 10))
    swing_o  = float(bar.get("swing_origin",    fl))
    atr14    = float(bar.get("atr14",           1.0))
    rsi_1h   = float(bar.get("rsi_1h",          50))
    rsi_1d   = float(bar.get("rsi_1d",          50))
    micro    = float(bar.get("micro",            50))
    mini     = float(bar.get("mini",             50))
    flow     = float(bar.get("flow_score",       0))
    ema50_4h = float(bar.get("ema50_4h",         cl))
    pp_hi    = bar.get("prior_pivot_high",       None)
    pp_lo    = bar.get("prior_pivot_low",        None)
    session  = bar.get("session",               "OFFHOURS")

    inst_up  = instrument.upper()
    min_age  = _MIN_FRACTAL_AGE.get(inst_up, 4)
    min_sw   = _SWING_ATR_MIN.get(inst_up, 0.8)

    # ── Rule 1: Close beyond fractal (not just wick) ────────────────────────
    if d == 1:
        r1_pass = (cl > fl) and (cl0 <= fl)
    else:
        r1_pass = (cl < fl) and (cl0 >= fl)

    # ── Rule 2: Minimum fractal age ─────────────────────────────────────────
    fractal_age = cur_idx - f_idx
    r2_pass     = fractal_age >= min_age

    # ── Rule 3: Minimum swing size (ATR filter) ──────────────────────────────
    if d == 1:
        swing_size = fl - swing_o   # fractal_high - prior swing low
    else:
        swing_size = swing_o - fl   # prior swing high - fractal_low
    r3_pass = (swing_size >= min_sw * atr14) if atr14 > 0 else False

    # ── Rule 4: Body quality (BOS candle must have meaningful body) ──────────
    body       = abs(cl - op)
    full_range = hi - lo
    r4_pass    = (body >= BOS_BODY_MIN_FRAC * full_range) if full_range > 0 else False

    # ── Rule 5: No immediate reversal (checked by caller on next bar) ────────
    # Here we record the level that must hold on bar+1
    # Caller in state machine should invalidate BOS if next bar closes back
    # through fractal_level. We mark r5_pass = True at BOS bar (deferred check).
    r5_pass = True   # stage machine enforces this in Stage 2

    rules = {
        "r1_close_beyond": r1_pass,
        "r2_min_age":       r2_pass,
        "r3_swing_size":    r3_pass,
        "r4_body_quality":  r4_pass,
        "r5_no_reversal":   r5_pass,
    }
    valid = r1_pass and r2_pass and r3_pass and r4_pass

    reasons = []
    if not r1_pass: reasons.append("no close beyond fractal")
    if not r2_pass: reasons.append(f"fractal too young ({fractal_age} < {min_age} bars)")
    if not r3_pass: reasons.append(f"swing too small ({swing_size:.4f} < {min_sw:.1f}x ATR)")
    if not r4_pass: reasons.append(f"weak body ({body:.4f} < 30% of range)")

    # ── 7-Pillar Importance Score ────────────────────────────────────────────
    # Pillar 1: Sweep involved (equal high/low swept before BOS)
    sweep_thresh = 0.3 * atr14
    if d == 1:
        p1 = (pp_hi is not None and
              not np.isnan(float(pp_hi)) and
              abs(fl - float(pp_hi)) < sweep_thresh)
    else:
        p1 = (pp_lo is not None and
              not np.isnan(float(pp_lo)) and
              abs(fl - float(pp_lo)) < sweep_thresh)

    # Pillar 2: RSI state at BOS time (not at exhaustion extremes)
    if d == 1:
        p2 = 45.0 <= rsi_1h <= 75.0
    else:
        p2 = 25.0 <= rsi_1h <= 55.0

    # Pillar 3: Micro Completion >= 67 (pullback preceded BOS = healthy structure)
    p3 = micro >= 67.0

    # Pillar 4: Flow Score aligned
    if d == 1:
        p4 = flow >= 1.0
    else:
        p4 = flow <= -1.0

    # Pillar 5: EMA position (price on correct side of 4H 50 EMA)
    if d == 1:
        p5 = cl > ema50_4h
    else:
        p5 = cl < ema50_4h

    # Pillar 6: Mini state supports (was exhausted before BOS, or recovering)
    if d == 1:
        p6 = mini < 40.0  # exhausted shorts = spring
    else:
        p6 = mini > 60.0  # exhausted longs = distribution

    # Pillar 7: Macro regime aligned (RSI 1D confirms direction)
    if d == 1:
        p7 = rsi_1d > 50.0
    else:
        p7 = rsi_1d < 50.0

    pillars = {
        "p1_sweep":         p1,
        "p2_rsi_state":     p2,
        "p3_micro_67":      p3,
        "p4_flow":          p4,
        "p5_ema_position":  p5,
        "p6_mini_supports": p6,
        "p7_macro_regime":  p7,
    }
    importance = sum(int(v) for v in pillars.values())

    # ── Grade from importance score ──────────────────────────────────────────
    if importance >= BOS_GRADE_AP:
        grade    = "A+"
        bos_type = "A"
    elif importance >= BOS_GRADE_A:
        grade    = "A"
        bos_type = "A"
    elif importance >= BOS_GRADE_B:
        grade    = "B"
        bos_type = "B"
    elif importance >= BOS_GRADE_C:
        grade    = "C"
        bos_type = "C"
    else:
        grade    = "SKIP"
        bos_type = "C"

    return {
        "valid":      valid,
        "importance": importance,
        "grade":      grade,
        "bos_type":   bos_type,
        "rules":      rules,
        "pillars":    pillars,
        "reason":     "; ".join(reasons) if reasons else "OK",
    }


# ══════════════════════════════════════════════════════════════════════════════
# 3. PULLBACK ZONE MEASUREMENT
#    Fibonacci-based depth zones after BOS confirmed.
# ══════════════════════════════════════════════════════════════════════════════

# Fibonacci zone boundaries
PULLBACK_ZONES = [
    (0.000, 0.236, 0),   # pre-zone (< shallow start) → not yet a pullback
    (0.236, 0.382, 1),   # Zone 1: Shallow — strong momentum
    (0.382, 0.618, 2),   # Zone 2: Standard — most reliable
    (0.618, 0.786, 3),   # Zone 3: Deep — elevated CHoCH risk
    (0.786, 9.999, 4),   # Zone 4: Danger — do NOT enter
]

# Instrument-specific typical depth (for guidance / note field)
_TYPICAL_ZONE: dict[str, tuple[int, int]] = {
    "JP225":  (2, 2),    # almost always Zone 2
    "DE40":   (2, 2),
    "UK100":  (1, 2),
    "HK50":   (2, 3),
    "US30":   (1, 2),
    "USTEC":  (1, 2),
    "SPX500": (1, 2),
    "XAUUSD": (2, 3),    # gold runs deeper
    "XAGUSD": (2, 3),
    "GBPUSD": (2, 2),
    "EURUSD": (2, 2),
    "GBPJPY": (2, 3),
    "EURJPY": (2, 2),
    "USDJPY": (2, 2),
    "AUDUSD": (2, 2),
    "USDCAD": (2, 2),
    "BTCUSD": (2, 3),
    "ETHUSD": (2, 3),
}


def f_get_pullback_zone(
    current_price: float,
    bos_level: float,
    swing_origin: float,
    direction: int,
    instrument: str,
    atr_val: float = 0.0,
) -> dict:
    """
    Measure current pullback depth after a BOS.

    Parameters:
      current_price : close of current bar (during pullback)
      bos_level     : fractal high (bull) or low (bear) that was broken
      swing_origin  : swing low (bull) or swing high (bear) before the BOS
                      (the 'origin' of the measured range)
      direction     : +1 bull, -1 bear
      instrument    : ticker string
      atr_val       : ATR(14) at current bar (for expected_depth_atr)

    Returns dict:
      zone              : 0-4
      zone_label        : 'PRE' / 'SHALLOW' / 'STANDARD' / 'DEEP' / 'DANGER'
      retrace_fib       : float 0.0-1.0+ (raw Fibonacci retrace ratio)
      is_valid          : False if zone == 4 (pullback too deep = BOS failing)
      in_entry_zone     : True when zone in (1, 2) [default] or zone == 3 if allowed
      typical_zone_min  : instrument's typical minimum zone
      typical_zone_max  : instrument's typical maximum zone
      expected_depth_atr: approximate pullback depth in ATR units (if atr_val > 0)
      invalidated       : True if retrace > 0.786 OR CHoCH detected (caller sets)
    """
    full_range = abs(bos_level - swing_origin)
    if full_range < 1e-10:
        return {
            "zone": 0, "zone_label": "PRE", "retrace_fib": 0.0,
            "is_valid": True, "in_entry_zone": False,
            "typical_zone_min": 2, "typical_zone_max": 2,
            "expected_depth_atr": 0.0, "invalidated": False,
        }

    # Retrace as fraction of full swing
    if direction == 1:
        retrace = (bos_level - current_price) / full_range
    else:
        retrace = (current_price - bos_level) / full_range
    retrace = max(retrace, 0.0)

    # Classify zone
    zone_num   = 0
    zone_label = "PRE"
    for lo_fib, hi_fib, znum in PULLBACK_ZONES:
        if lo_fib <= retrace < hi_fib:
            zone_num   = znum
            zone_label = ["PRE", "SHALLOW", "STANDARD", "DEEP", "DANGER"][znum]
            break

    # Instrument typical depth
    tz_min, tz_max = _TYPICAL_ZONE.get(instrument.upper(), (2, 2))

    # Valid if not in Zone 4
    is_valid     = zone_num < 4
    invalidated  = retrace > 0.786

    # In entry zone: Zone 1-2 by default, Zone 3 is allowed but requires stricter score
    in_entry_zone = zone_num in (1, 2, 3) and is_valid

    # Approximate depth in ATR units
    expected_depth_atr = 0.0
    if atr_val > 0:
        expected_depth_atr = round(retrace * full_range / atr_val, 2)

    return {
        "zone":               zone_num,
        "zone_label":         zone_label,
        "retrace_fib":        round(retrace, 4),
        "is_valid":           is_valid,
        "in_entry_zone":      in_entry_zone,
        "typical_zone_min":   tz_min,
        "typical_zone_max":   tz_max,
        "expected_depth_atr": expected_depth_atr,
        "invalidated":        invalidated,
    }


# ══════════════════════════════════════════════════════════════════════════════
# 4. PULLBACK END SCORE (5 conditions)
#    Detects when a pullback is complete and entry is ready.
#    Uses instrument-specific thresholds where noted.
# ══════════════════════════════════════════════════════════════════════════════

# Per-instrument condition thresholds (overrides)
# Format: { instrument: { condition: threshold } }
# Unspecified instruments use defaults.
_END_SCORE_THRESHOLDS: dict[str, dict[str, float]] = {
    # Default embedded in function body below
    "XAUUSD": {
        "mini_os":    40.0,   # mini must be < 40 (default 45) — gold needs deeper exhaustion
        "rsi30_os":   40.0,
        "micro_min":  65.0,   # micro >= 65 (default 60) — gold pullbacks are deeper
        "phase6_micro": 70.0,
        "phase6_mini":  40.0,
        "phase6_rsi2h": 45.0,
    },
    "GBPJPY": {
        "mini_os":    40.0,
        "rsi30_os":   40.0,
        "micro_min":  65.0,
        "phase6_micro": 70.0,
        "phase6_mini":  40.0,
        "phase6_rsi2h": 45.0,
    },
    "JP225": {
        "mini_os":    40.0,
        "rsi30_os":   40.0,
        "micro_min":  60.0,
        "phase6_micro": 65.0,
        "phase6_mini":  45.0,
        "phase6_rsi2h": 50.0,
    },
}

_END_SCORE_DEFAULT: dict[str, float] = {
    "mini_os":      45.0,   # Condition A: mini < X (oversold on bull)
    "rsi30_os":     45.0,   # Condition B: rsi_30m < X
    "micro_min":    60.0,   # Condition C: micro >= X
    "phase6_micro": 65.0,   # Condition E: journey phase 6 micro threshold
    "phase6_mini":  45.0,   # Condition E: journey phase 6 mini threshold
    "phase6_rsi2h": 50.0,   # Condition E: journey phase 6 rsi_2h threshold
}


def f_check_pullback_end(
    bar: dict,
    instrument: str,
    tf: str,
    direction: int,
    pullback_zone: int = 2,
) -> dict:
    """
    Evaluate whether the pullback has ended and entry is ready.

    bar dict keys expected:
      mini          : Mini Composite RSI (current bar)
      mini_prev     : Mini Composite RSI (1 bar ago)
      mini_prev2    : Mini Composite RSI (2 bars ago) — for slope
      rsi_30m       : 30m RSI (falls back to mini if unavailable)
      rsi_30m_prev  : 30m RSI (1 bar ago)
      micro         : Micro Completion (0-100)
      flow          : current Flow Score
      flow_3bars    : Flow Score 3 bars ago
      rsi_2h        : 2H RSI (current)
      rsi_2h_prev   : 2H RSI (1 bar ago)

    pullback_zone: current zone (3 requires stricter end score)

    Returns dict:
      end_score     : int 0-5
      conditions    : dict {A: bool, B: bool, C: bool, D: bool, E: bool}
      ready         : True if end_score >= min_end_score for this instrument/zone
      min_required  : the threshold used
    """
    thr = {**_END_SCORE_DEFAULT, **_END_SCORE_THRESHOLDS.get(instrument.upper(), {})}

    mn    = float(bar.get("mini",       50))
    mn1   = float(bar.get("mini_prev",  mn))
    mn2   = float(bar.get("mini_prev2", mn1))
    r30   = float(bar.get("rsi_30m",    mn))   # fallback to mini
    r30_1 = float(bar.get("rsi_30m_prev", r30))
    mic   = float(bar.get("micro",      0))
    fl    = float(bar.get("flow",       0))
    fl3   = float(bar.get("flow_3bars", fl))
    r2h   = float(bar.get("rsi_2h",     50))
    r2h1  = float(bar.get("rsi_2h_prev", r2h))

    t_mini    = thr["mini_os"]
    t_r30     = thr["rsi30_os"]
    t_micro   = thr["micro_min"]
    t6_micro  = thr["phase6_micro"]
    t6_mini   = thr["phase6_mini"]
    t6_rsi2h  = thr["phase6_rsi2h"]

    # Condition A: Mini exhausted AND recovering (slope up on bull)
    if direction == 1:
        cA = (mn < t_mini) and (mn > mn2)
    else:
        cA = (mn > (100 - t_mini)) and (mn < mn2)

    # Condition B: RSI 30m exhausted AND turning
    if direction == 1:
        cB = (r30 < t_r30) and (r30 > r30_1)
    else:
        cB = (r30 > (100 - t_r30)) and (r30 < r30_1)

    # Condition C: Micro completion mature
    cC = mic >= t_micro

    # Condition D: Flow flip (was against direction, now neutral/with)
    if direction == 1:
        cD = (fl3 < 0) and (fl >= 0)
    else:
        cD = (fl3 > 0) and (fl <= 0)

    # Condition E: Journey Phase 6 — full cycle exhaustion signal
    #   Bull: micro >= t6_micro AND mini < t6_mini AND mini slope up AND rsi_2h < t6_rsi2h AND rsi_2h rising
    #   Bear: mirror
    if direction == 1:
        cE = (mic >= t6_micro and mn < t6_mini and mn > mn2 and
              r2h < t6_rsi2h and r2h > r2h1)
    else:
        cE = (mic >= t6_micro and mn > (100 - t6_mini) and mn < mn2 and
              r2h > (100 - t6_rsi2h) and r2h < r2h1)

    conditions = {"A": cA, "B": cB, "C": cC, "D": cD, "E": cE}
    end_score  = sum(int(v) for v in conditions.values())

    # Minimum end score: base from instrument entry rules, +1 for Zone 3
    base_rules    = f_get_entry_rules(instrument)
    min_required  = base_rules["min_end_score"]
    if pullback_zone == 3:
        min_required += 1   # Zone 3 always needs 1 extra condition

    return {
        "end_score":    end_score,
        "conditions":   conditions,
        "ready":        end_score >= min_required,
        "min_required": min_required,
    }


# ══════════════════════════════════════════════════════════════════════════════
# 5. ENTRY RULES PER INSTRUMENT
#    min_end_score, min_bos_score, sl_atr_mult, tp_multiples, valid_sessions
# ══════════════════════════════════════════════════════════════════════════════

# Session constants (UTC hours)
SESSION_TOKYO   = ("TOKYO",   "OVERLAP")              # 00:00-08:00 UTC
SESSION_LONDON  = ("LONDON",  "OVERLAP")              # 07:00-16:00 UTC
SESSION_NY      = ("NY",      "OVERLAP")              # 13:00-20:00 UTC
SESSION_ANY     = ("TOKYO",   "LONDON", "NY", "OVERLAP")

_ENTRY_RULES: dict[str, dict] = {
    "JP225": {
        # v3 recalibration (Task 5): sl_atr_mult=2.0, tp_type_a=2.0, tp_type_b=1.5,
        # min_end_score=4. Wider SL reduces early stop-outs on volatile opens.
        # Lower TP multiples (2.0/1.5) target realistic intraday swing range.
        # min_end_score=4 enforces stricter pullback completion requirement.
        "min_end_score":  4,
        "min_bos_score":  4,       # B grade minimum (importance >= 4)
        "session_bos_override": {"TOKYO": 5},  # Tokyo BOS requires A grade
        "sl_atr_mult":    2.0,     # v3: widened from 1.5 — absorbs JP225 gap risk
        "tp_type_a":      2.0,     # v3: realistic intraday target (was 3.0)
        "tp_type_b":      1.5,     # v3: conservative target (was 2.5)
        "valid_sessions": ["NY", "LONDON", "OVERLAP", "TOKYO"],
        "notes":          "v3 recalibration: wider SL, lower TP, stricter entry filter.",
    },
    "DE40": {
        "min_end_score":  3,
        "min_bos_score":  3,
        "session_bos_override": {},
        "sl_atr_mult":    1.5,
        "tp_type_a":      3.0,
        "tp_type_b":      2.5,
        "valid_sessions": ["LONDON", "OVERLAP"],
        "notes":          "London session only. Close all before London close (18:00 SAST).",
    },
    "UK100": {
        "min_end_score":  3,
        "min_bos_score":  3,
        "session_bos_override": {},
        "sl_atr_mult":    1.5,
        "tp_type_a":      3.0,
        "tp_type_b":      2.5,
        "valid_sessions": ["LONDON", "OVERLAP"],
        "notes":          "London session only. Edge disappears after London close.",
    },
    "HK50": {
        "min_end_score":  3,
        "min_bos_score":  3,
        "session_bos_override": {},
        "sl_atr_mult":    1.5,
        "tp_type_a":      3.0,
        "tp_type_b":      2.5,
        "valid_sessions": ["TOKYO", "LONDON", "OVERLAP"],
        "notes":          "Asian session primary.",
    },
    "US30": {
        "min_end_score":  3,
        "min_bos_score":  3,
        "session_bos_override": {},
        "sl_atr_mult":    1.5,
        "tp_type_a":      3.0,
        "tp_type_b":      2.5,
        "valid_sessions": ["NY", "OVERLAP"],
        "notes":          "NY only. Shallow pullbacks (Zone 1) common and valid. Best index.",
    },
    "USTEC": {
        "min_end_score":  3,
        "min_bos_score":  3,
        "session_bos_override": {},
        "sl_atr_mult":    1.5,
        "tp_type_a":      3.0,
        "tp_type_b":      2.5,
        "valid_sessions": ["NY", "OVERLAP"],
        "notes":          "NY only. 4H BOS + 1H entry = best combination.",
    },
    "SPX500": {
        "min_end_score":  3,
        "min_bos_score":  3,
        "session_bos_override": {},
        "sl_atr_mult":    1.5,
        "tp_type_a":      3.0,
        "tp_type_b":      2.5,
        "valid_sessions": ["NY", "OVERLAP"],
        "notes":          "Same family as US30. NY only.",
    },
    "XAUUSD": {
        "min_end_score":  4,       # stricter — gold fakes frequently
        "min_bos_score":  3,
        "session_bos_override": {},
        "sl_atr_mult":    1.5,
        "tp_type_a":      3.0,
        "tp_type_b":      2.5,
        "valid_sessions": ["LONDON", "NY", "OVERLAP"],
        "notes":          "London/NY overlap best. Always wait Zone 2+. Never rush gold.",
    },
    "XAGUSD": {
        "min_end_score":  3,
        "min_bos_score":  3,
        "session_bos_override": {},
        "sl_atr_mult":    1.5,
        "tp_type_a":      3.0,
        "tp_type_b":      2.5,
        "valid_sessions": ["LONDON", "NY", "OVERLAP"],
        "notes":          "More volatile than gold. Right bars=3 on 15m.",
    },
    "GBPUSD": {
        "min_end_score":  3,       # 3 on 15M, 4 on 1H (handled by caller + TF check)
        "min_bos_score":  3,
        "session_bos_override": {},
        "sl_atr_mult":    1.5,
        "tp_type_a":      3.0,
        "tp_type_b":      2.5,
        "valid_sessions": ["LONDON", "NY", "OVERLAP"],
        "notes":          "Asian range sweep + London open = almost always A/B quality.",
    },
    "EURUSD": {
        "min_end_score":  3,
        "min_bos_score":  3,
        "session_bos_override": {},
        "sl_atr_mult":    1.5,
        "tp_type_a":      3.0,
        "tp_type_b":      2.5,
        "valid_sessions": ["LONDON", "NY", "OVERLAP"],
        "notes":          "Standard measured forex.",
    },
    "GBPJPY": {
        "min_end_score":  4,       # mandatory — high reward, high risk
        "min_bos_score":  5,       # A+ only (importance >= 5)
        "session_bos_override": {},
        "sl_atr_mult":    2.0,     # wider SL — GBPJPY is explosive
        "tp_type_a":      3.5,
        "tp_type_b":      3.0,
        "valid_sessions": ["LONDON", "NY", "OVERLAP"],
        "notes":          "A+ only. End score >= 4 mandatory. Wider SL (2.0x ATR).",
    },
    "EURJPY": {
        "min_end_score":  3,
        "min_bos_score":  3,
        "session_bos_override": {},
        "sl_atr_mult":    1.5,
        "tp_type_a":      3.0,
        "tp_type_b":      2.5,
        "valid_sessions": ["LONDON", "NY", "OVERLAP"],
        "notes":          "Safest instrument — zero ruin probability. Good for learning.",
    },
    "USDJPY": {
        "min_end_score":  3,
        "min_bos_score":  3,
        "session_bos_override": {},
        "sl_atr_mult":    1.5,
        "tp_type_a":      3.0,
        "tp_type_b":      2.5,
        "valid_sessions": ["LONDON", "NY", "OVERLAP"],
        "notes":          "Measured forex. Tokyo session sometimes valid.",
    },
    "AUDUSD": {
        "min_end_score":  3,
        "min_bos_score":  3,
        "session_bos_override": {},
        "sl_atr_mult":    1.5,
        "tp_type_a":      3.0,
        "tp_type_b":      2.5,
        "valid_sessions": ["LONDON", "NY", "OVERLAP"],
        "notes":          "Standard forex.",
    },
    "USDCAD": {
        "min_end_score":  3,
        "min_bos_score":  3,
        "session_bos_override": {},
        "sl_atr_mult":    1.5,
        "tp_type_a":      3.0,
        "tp_type_b":      2.5,
        "valid_sessions": ["LONDON", "NY", "OVERLAP"],
        "notes":          "Standard forex. Best Tier 2 in Phase 1A backtest.",
    },
    "BTCUSD": {
        "min_end_score":  4,
        "min_bos_score":  3,
        "session_bos_override": {},
        "sl_atr_mult":    1.5,
        "tp_type_a":      3.0,
        "tp_type_b":      2.5,
        "valid_sessions": list(SESSION_ANY),
        "notes":          "24/7 market. Never enter on Zone 1 — always Zone 2+ for crypto.",
    },
    "ETHUSD": {
        "min_end_score":  4,
        "min_bos_score":  3,
        "session_bos_override": {},
        "sl_atr_mult":    1.5,
        "tp_type_a":      3.0,
        "tp_type_b":      2.5,
        "valid_sessions": list(SESSION_ANY),
        "notes":          "24/7 market. Deeper pullbacks than BTC typically.",
    },
}

_ENTRY_DEFAULT: dict = {
    "min_end_score":  3,
    "min_bos_score":  3,
    "session_bos_override": {},
    "sl_atr_mult":    1.5,
    "tp_type_a":      3.0,
    "tp_type_b":      2.5,
    "valid_sessions": ["LONDON", "NY", "OVERLAP"],
    "notes":          "Default — not specifically calibrated.",
}


def f_get_entry_rules(instrument: str) -> dict:
    """
    Returns entry rule parameters for the given instrument.

    Returns dict:
      min_end_score          : int — pullback end score threshold
      min_bos_score          : int — minimum BOS importance to act on
      session_bos_override   : dict {session_name: min_bos_score} — session-specific overrides
      sl_atr_mult            : float — stop-loss = sl_atr_mult × ATR(14) from entry
      tp_type_a              : float — TP2 R-multiple for Type A BOS
      tp_type_b              : float — TP2 R-multiple for Type B BOS
      valid_sessions         : list[str] — sessions where entries are valid
      notes                  : str — human-readable notes

    Example:
        rules = f_get_entry_rules("GBPJPY")
        # -> min_end_score=4, min_bos_score=5, sl_atr_mult=2.0, valid_sessions=["LONDON","NY","OVERLAP"]
    """
    return {**_ENTRY_DEFAULT, **_ENTRY_RULES.get(instrument.upper(), {})}


def f_check_session_valid(instrument: str, session: str) -> bool:
    """
    Returns True if this session is valid for trading this instrument.
    """
    rules = f_get_entry_rules(instrument)
    return session.upper() in [s.upper() for s in rules["valid_sessions"]]


def f_get_min_bos_for_session(instrument: str, session: str) -> int:
    """
    Returns the minimum BOS importance score for this instrument+session combo.
    Accounts for session-specific overrides (e.g. JP225 Tokyo = 5).
    """
    rules    = f_get_entry_rules(instrument)
    override = rules.get("session_bos_override", {})
    return override.get(session.upper(), rules["min_bos_score"])


# ══════════════════════════════════════════════════════════════════════════════
# 6. VOLUME AVAILABILITY — instruments where MT5 reports zero/no volume
#    (cash indices on IC Markets do not stream volume).
#    For these instruments: volume-dependent signals are replaced by
#    price-action proxies (ATR range, wick size, bar structure).
# ══════════════════════════════════════════════════════════════════════════════

VOLUME_UNAVAILABLE: list[str] = [
    "UK100",   # FTSE 100 cash — MT5 reports no volume
    "DE40",    # DAX 40 cash  — MT5 reports no volume
    "FTSE",    # alias
    "DAX",     # alias
]


def f_has_volume(instrument: str) -> bool:
    """
    Returns False if the instrument is known to have no meaningful volume data.
    Used by macro_structure_engine, pullback_engine, and liquidity_mapper to
    switch volume checks to price-action proxies.
    """
    return instrument.upper() not in [i.upper() for i in VOLUME_UNAVAILABLE]


# ══════════════════════════════════════════════════════════════════════════════
# 7. UTILITY HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _normalise_tf(tf: str) -> str:
    """Normalise timeframe string to canonical form used in the table."""
    tf = tf.strip().upper()
    _MAP = {
        "1H": "1H", "H1": "1H", "60": "1H", "60M": "1H",
        "4H": "4H", "H4": "4H", "240": "4H", "240M": "4H",
        "15M": "15m", "15": "15m", "M15": "15m",
        "5M": "5m",  "5": "5m",  "M5": "5m",
        "D": "D", "1D": "D", "DAY": "D", "DAILY": "D",
        "W": "W", "1W": "W", "WEEK": "W", "WEEKLY": "W",
    }
    return _MAP.get(tf, tf.lower())


def get_all_instruments() -> list[str]:
    """Return all instruments with explicit calibration."""
    return list(_FRACTAL_TABLE.keys())


def print_calibration_summary():
    """Print a human-readable summary of all calibration values."""
    import io as _io
    out = _io.StringIO()

    print("=" * 90, file=out)
    print("  GOLDEN HIGHWAY -- STRUCTURE ENGINE CALIBRATION SUMMARY", file=out)
    print("=" * 90, file=out)
    print(f"\n  {'INSTRUMENT':<10} {'TF':<5} {'LEFT':>5} {'RIGHT':>6} {'MIN_AGE':>8} {'MIN_SWING_ATR':>14}  {'MIN_END':>8} {'MIN_BOS':>8} {'SL_MULT':>8}  {'TP_A':>5} {'TP_B':>5}", file=out)
    print("  " + "-" * 88, file=out)

    for inst in sorted(_FRACTAL_TABLE.keys()):
        rules = f_get_entry_rules(inst)
        for tf_key in ("1H", "4H", "15m", "5m"):
            params = _FRACTAL_TABLE[inst].get(tf_key)
            if params:
                left, right, min_age, min_sw = params
                print(f"  {inst:<10} {tf_key:<5} {left:>5} {right:>6} {min_age:>8} {min_sw:>14.1f}  "
                      f"{rules['min_end_score']:>8} {rules['min_bos_score']:>8} "
                      f"{rules['sl_atr_mult']:>8.1f}  "
                      f"{rules['tp_type_a']:>5.1f} {rules['tp_type_b']:>5.1f}",
                      file=out)

    print("\n  BOS Grade Scale:", file=out)
    print("    6-7 pillars = A+ (full size)   |  5 = A (standard)  |  4 = B (reduced)", file=out)
    print("    3 = C (wait for extra confirm) |  0-2 = SKIP", file=out)
    print("\n  Pullback Zones (Fibonacci):", file=out)
    print("    Zone 1 Shallow:   0.236-0.382  |  Zone 2 Standard: 0.382-0.618", file=out)
    print("    Zone 3 Deep:      0.618-0.786  |  Zone 4 DANGER:   > 0.786 (do not enter)", file=out)
    print("=" * 90, file=out)

    print(out.getvalue())


# ══════════════════════════════════════════════════════════════════════════════
# 7. PINE SCRIPT MIRROR TABLE
#    Generates the Pine Script switch/if-else block for instrument detection.
#    Called by the Pine Script build script to keep Pine in sync with Python.
# ══════════════════════════════════════════════════════════════════════════════

def generate_pine_fractal_block() -> str:
    """
    Generate the Pine Script v6 instrument detection block for fractal params.
    Output is a ready-to-paste Pine Script string.
    """
    lines = [
        "// ── Auto-generated by structure_engine.py -- do not edit manually ──",
        "// f_get_fractal_params() mirror for Pine Script",
        "get_fractal_left(tf_str) =>",
        "    t = timeframe.period",
        "    sym = syminfo.ticker",
    ]
    # Build nested if-else for each instrument
    # We handle 1H and 4H only for Pine (primary TFs)
    for i, (inst, tfs) in enumerate(_FRACTAL_TABLE.items()):
        cond = "if" if i == 0 else "else if"
        left_1h  = tfs.get("1H",  (7, 3, 4, 0.8))[0]
        left_4h  = tfs.get("4H",  (8, 3, 5, 0.8))[0]
        left_15m = tfs.get("15m", (5, 2, 3, 0.6))[0]
        lines.append(
            f'    {cond} str.contains(sym, "{inst}")\n'
            f'        t == "60" ? {left_1h} : t == "240" ? {left_4h} : {left_15m}'
        )
    lines.append("    else")
    lines.append("        t == \"60\" ? 7 : t == \"240\" ? 8 : 5")
    lines.append("")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# Quick self-test when run directly
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import io as _io, sys as _sys
    _sys.stdout = _io.TextIOWrapper(_sys.stdout.buffer, encoding="utf-8", errors="replace")

    print_calibration_summary()

    # Spot-check a few values
    assert f_get_fractal_params("XAUUSD", "4H")  == (10, 3, 5, 1.0),  "XAUUSD 4H mismatch"
    assert f_get_fractal_params("GBPUSD", "1H")  == (7,  3, 4, 0.6),  "GBPUSD 1H mismatch"
    assert f_get_fractal_params("JP225",  "1H")  == (8,  3, 5, 1.0),  "JP225 1H mismatch"
    assert f_get_fractal_params("US30",   "5m")  == (2,  2, 2, 0.5),  "US30 5m mismatch"
    assert f_get_fractal_params("UNKNOWN","1H")  == (7,  3, 4, 0.8),  "DEFAULT 1H mismatch"

    rules_gbpjpy = f_get_entry_rules("GBPJPY")
    assert rules_gbpjpy["min_end_score"] == 4,   "GBPJPY end score mismatch"
    assert rules_gbpjpy["min_bos_score"] == 5,   "GBPJPY bos score mismatch"
    assert rules_gbpjpy["sl_atr_mult"]   == 2.0, "GBPJPY SL mult mismatch"

    rules_xau = f_get_entry_rules("XAUUSD")
    assert rules_xau["min_end_score"] == 4, "XAUUSD end score mismatch"

    assert f_check_session_valid("DE40",  "NY")     == False, "DE40 NY should be invalid"
    assert f_check_session_valid("DE40",  "LONDON") == True,  "DE40 London should be valid"
    assert f_check_session_valid("US30",  "NY")     == True,  "US30 NY should be valid"
    assert f_get_min_bos_for_session("JP225", "TOKYO") == 5,  "JP225 Tokyo override"
    assert f_get_min_bos_for_session("JP225", "NY")    == 4,  "JP225 NY default"

    # Zone test
    # full_range = 1.1000 - 1.0800 = 0.0200
    # Zone 2 (Standard 0.382-0.618): price = 1.1000 - 0.50*0.02 = 1.0900 -> retrace=0.50
    z = f_get_pullback_zone(
        current_price=1.0900, bos_level=1.1000, swing_origin=1.0800,
        direction=1, instrument="EURUSD", atr_val=0.0010
    )
    assert z["zone"] == 2, f"Expected Zone 2, got {z['zone']} (retrace={z['retrace_fib']})"

    # Zone 4 (Danger >0.786): price = 1.1000 - 0.80*0.02 = 1.0840 -> retrace=0.80
    z2 = f_get_pullback_zone(
        current_price=1.0840, bos_level=1.1000, swing_origin=1.0800,
        direction=1, instrument="EURUSD", atr_val=0.0010
    )
    assert z2["zone"] == 4, f"Expected Zone 4 (danger), got {z2['zone']} (retrace={z2['retrace_fib']})"

    # BOS classify test
    bos = f_classify_bos("EURUSD", "1H", {
        "direction": 1, "close": 1.1005, "open": 1.0990,
        "high": 1.1010, "low": 1.0985, "close_prev": 1.0995,
        "fractal_level": 1.1000, "fractal_bar_idx": 10, "current_bar_idx": 20,
        "swing_origin": 1.0900, "atr14": 0.0015,
        "rsi_1h": 58, "rsi_1d": 62, "micro": 72, "mini": 35,
        "flow_score": 2, "ema50_4h": 1.0980,
        "prior_pivot_high": 1.0998, "session": "LONDON",
    })
    print(f"\n  BOS classify test (EURUSD 1H): valid={bos['valid']} "
          f"importance={bos['importance']} grade={bos['grade']} type={bos['bos_type']}")
    assert bos["valid"], "EURUSD BOS should be valid"

    print("\n  All assertions passed. structure_engine.py is correctly calibrated.")
