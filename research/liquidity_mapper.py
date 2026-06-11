"""
research/liquidity_mapper.py
=============================
Golden Highway Architecture v3 — Liquidity Level Mapper

Maps two classes of liquidity for every bar:

  INTERNAL LIQUIDITY (pullback targets — between current price and BOS):
    - Recent confirmed fractal lows/highs (last 100 bars)
    - Equal lows/highs clusters (within 0.5 ATR)
    - POC proxy: highest-volume price zone in lookback
    - Previous consolidation midpoints

  EXTERNAL LIQUIDITY (expansion targets — beyond current BOS level):
    - Prior pivot highs/lows (prior_pivot_high / prior_pivot_low columns)
    - Rolling 50/100-bar extremes
    - Round number clusters (auto-scaled to instrument price)
    - HTF fractal levels (from 4H / Daily RSI context)

Each level is assigned a STRENGTH score 0–3:
    0 = weak  (single touch, no round number)
    1 = moderate (touched 2+ times OR near round number)
    2 = strong (touched 3+ times OR exact round number)
    3 = maximum (equal highs/lows cluster + round number)

For instruments without volume (UK100, DE40):
    POC is replaced by the midpoint of the widest price range in lookback.
    All other detection uses price action only.

Column map (from build_features output):
    close / high / low / open / volume
    atr14, vol_ratio, vol_avg20
    last_pivot_high, last_pivot_low
    prior_pivot_high, prior_pivot_low
    rsi_1d, htf_align
"""

from __future__ import annotations

import sys, warnings
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd
from pathlib import Path
from dataclasses import dataclass, field

sys.path.insert(0, str(Path(__file__).resolve().parent))
from structure_engine import f_has_volume


# ══════════════════════════════════════════════════════════════════════════════
# 1. DATA STRUCTURES
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class LiqLevel:
    """A single liquidity level with metadata."""
    price:    float
    strength: int          # 0–3
    liq_type: str          # "internal" | "external"
    source:   str          # "fractal_low" | "equal_lows" | "poc" | "round" | "prior_pivot" | "htf"
    touches:  int = 1
    note:     str = ""

    def __repr__(self) -> str:
        stars = "★" * self.strength + "☆" * (3 - self.strength)
        return f"  {self.price:>12,.4f}  [{stars}] {self.source:<18} {self.note}"


@dataclass
class LiquidityMap:
    """Complete liquidity map for one bar."""
    instrument:        str
    bar_idx:           int
    datetime_utc:      str
    direction:         int          # +1 bull / -1 bear
    close:             float
    bos_level:         float
    atr:               float

    internal_levels:   list[LiqLevel] = field(default_factory=list)
    external_levels:   list[LiqLevel] = field(default_factory=list)
    nearest_internal:  float = np.nan
    nearest_external:  float = np.nan
    htf_target:        float = np.nan
    rr_available:      float = np.nan
    valid:             bool  = True    # False if RR < 2.0
    narrative:         str  = ""

    # Suggested trade params
    suggested_sl:      float = np.nan
    suggested_tp1:     float = np.nan
    suggested_tp2:     float = np.nan


# ══════════════════════════════════════════════════════════════════════════════
# 2. HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _round_number_proximity(price: float, atr: float) -> tuple[bool, float]:
    """
    Detect if price is within 0.5 ATR of a round number.
    Round numbers are auto-scaled:
        price < 10    → 0.5 and 1.0
        price < 100   → 5, 10
        price < 1000  → 25, 50, 100
        price < 10000 → 100, 250, 500, 1000
        price >= 10000→ 500, 1000, 5000
    Returns (is_near_round, nearest_round_price).
    """
    if price <= 0:
        return False, price

    if price < 10:
        increments = [0.5, 1.0]
    elif price < 100:
        increments = [5.0, 10.0]
    elif price < 1000:
        increments = [25.0, 50.0, 100.0]
    elif price < 10_000:
        increments = [100.0, 250.0, 500.0, 1000.0]
    else:
        increments = [500.0, 1000.0, 5000.0]

    best_dist  = float("inf")
    best_round = price
    for inc in increments:
        nearest = round(price / inc) * inc
        dist    = abs(price - nearest)
        if dist < best_dist:
            best_dist  = dist
            best_round = nearest

    return (best_dist <= 0.5 * atr), best_round


def _scan_pivots(arr: np.ndarray, left: int = 5, right: int = 3) -> np.ndarray:
    """
    Re-scan an OHLC array for pivot highs or lows.
    arr = highs (for pivot highs) or lows (for pivot lows).
    Returns array of nan with pivot values at their bar positions.
    """
    N   = len(arr)
    out = np.full(N, np.nan)
    for j in range(left, N - right):
        v = arr[j]
        if all(v >= arr[j - k] for k in range(1, left + 1)) and \
           all(v >= arr[j + k] for k in range(1, right + 1)):
            out[j] = v
    return out


def _scan_pivot_lows(arr: np.ndarray, left: int = 5, right: int = 3) -> np.ndarray:
    N   = len(arr)
    out = np.full(N, np.nan)
    for j in range(left, N - right):
        v = arr[j]
        if all(v <= arr[j - k] for k in range(1, left + 1)) and \
           all(v <= arr[j + k] for k in range(1, right + 1)):
            out[j] = v
    return out


def _cluster_levels(levels: list[float], tol: float) -> list[tuple[float, int]]:
    """
    Cluster nearby price levels within tolerance `tol`.
    Returns list of (cluster_mean, touch_count) sorted by price desc.
    """
    if not levels:
        return []
    sorted_l = sorted(levels)
    clusters: list[list[float]] = [[sorted_l[0]]]
    for lv in sorted_l[1:]:
        if lv - clusters[-1][-1] <= tol:
            clusters[-1].append(lv)
        else:
            clusters.append([lv])
    return sorted(
        [(float(np.mean(c)), len(c)) for c in clusters],
        key=lambda x: x[0], reverse=True
    )


def f_calculate_rr(entry: float, sl: float, tp: float) -> float:
    """Reward:risk ratio. Returns 0.0 if risk is zero."""
    risk   = abs(entry - sl)
    reward = abs(tp - entry)
    if risk <= 0:
        return 0.0
    return round(reward / risk, 2)


# ══════════════════════════════════════════════════════════════════════════════
# 3. INTERNAL LIQUIDITY MAPPER
# ══════════════════════════════════════════════════════════════════════════════

def f_map_internal_liquidity(
    df:          pd.DataFrame,
    bar_idx:     int,
    direction:   int,
    bos_level:   float,
    instrument:  str,
    lookback:    int = 100,
) -> tuple[list[LiqLevel], float]:
    """
    Map internal liquidity levels: levels on the OPPOSITE side of price
    from the trade direction — these are pullback / stop-sweep targets.

    Definition:
      BULL (direction=+1): internal = fractal LOWS below current price
                           (stop clusters that could be swept on pullback)
      BEAR (direction=-1): internal = fractal HIGHS above current price
                           (equal highs that could be swept on pullback)

    The lower bound for BULL internal is max(bos_level - 5*ATR, price - 15*ATR)
    to keep the zone relevant without going too deep into history.

    Returns (levels_list, nearest_internal_price).
    nearest_internal is the CLOSEST level to current price on the opposite side.
    """
    start   = max(0, bar_idx - lookback)
    end_i   = bar_idx          # no lookahead

    close   = float(df["close"].iloc[bar_idx])
    atr     = max(float(df["atr14"].iloc[bar_idx]), 1e-6)
    highs   = df["high"].values[start:end_i]
    lows    = df["low"].values[start:end_i]
    vols    = df["volume"].values[start:end_i] if "volume" in df.columns else np.ones(end_i - start)
    has_vol = f_has_volume(instrument)

    tol     = atr * 0.5          # cluster tolerance

    # Zone boundaries
    if direction == +1:
        zone_lo = max(bos_level - 5.0 * atr, close - 15.0 * atr)
        zone_hi = close - tol   # strictly below price
    else:
        zone_lo = close + tol   # strictly above price
        zone_hi = min(bos_level + 5.0 * atr, close + 15.0 * atr)

    raw_levels: list[float] = []

    # ── Fractal lows (bull) / fractal highs (bear) ─────────────────────
    if direction == +1:
        piv = _scan_pivot_lows(lows, left=3, right=2)
        for v in piv:
            if not np.isnan(v) and zone_lo <= v <= zone_hi:
                raw_levels.append(float(v))
    else:
        piv = _scan_pivots(highs, left=3, right=2)
        for v in piv:
            if not np.isnan(v) and zone_lo <= v <= zone_hi:
                raw_levels.append(float(v))

    # ── BOS level itself is a key internal reference ──────────────────
    if direction == +1 and zone_lo <= bos_level <= zone_hi:
        raw_levels.append(bos_level)
    elif direction == -1 and zone_lo <= bos_level <= zone_hi:
        raw_levels.append(bos_level)

    # ── prior_pivot_low (bull) / prior_pivot_high (bear) ─────────────
    if direction == +1:
        ppl = float(df["prior_pivot_low"].values[bar_idx]) if "prior_pivot_low" in df.columns else np.nan
        if not np.isnan(ppl) and zone_lo <= ppl <= zone_hi:
            raw_levels.append(ppl)
    else:
        pph = float(df["prior_pivot_high"].values[bar_idx]) if "prior_pivot_high" in df.columns else np.nan
        if not np.isnan(pph) and zone_lo <= pph <= zone_hi:
            raw_levels.append(pph)

    # ── Cluster equal lows / equal highs ────────────────────────────────
    clustered = _cluster_levels(raw_levels, tol)

    # ── Build LiqLevel objects ────────────────────────────────────────────
    levels: list[LiqLevel] = []
    for price, touches in clustered:
        is_round, round_p = _round_number_proximity(price, atr)
        strength = min(3, (touches - 1) + (1 if is_round else 0) + (1 if touches >= 3 else 0))
        if price == bos_level:
            src = "bos_level"
        elif direction == +1:
            src = "equal_lows" if touches >= 2 else "fractal_low"
        else:
            src = "equal_highs" if touches >= 2 else "fractal_high"
        note  = (f"× {touches}" if touches >= 2 else "") + \
                (f" | near {round_p:,.1f}" if is_round else "")
        levels.append(LiqLevel(price=price, strength=strength, liq_type="internal",
                                source=src, touches=touches, note=note))

    # ── POC proxy ─────────────────────────────────────────────────────────
    poc_price = np.nan
    if len(highs) > 5:
        if has_vol and np.sum(vols) > 0:
            bins      = 20
            price_min = float(np.min(lows))
            price_max = float(np.max(highs))
            if price_max > price_min:
                bin_size    = (price_max - price_min) / bins
                vol_per_bin = np.zeros(bins)
                for j in range(len(highs)):
                    b_idx = min(int((lows[j] - price_min) / bin_size), bins - 1)
                    vol_per_bin[b_idx] += float(vols[j])
                poc_bin   = int(np.argmax(vol_per_bin))
                poc_price = price_min + (poc_bin + 0.5) * bin_size
        else:
            ranges    = highs - lows
            widest    = int(np.argmax(ranges))
            poc_price = float(lows[widest] + ranges[widest] / 2.0)

    if not np.isnan(poc_price) and zone_lo <= poc_price <= zone_hi:
        is_round, round_p = _round_number_proximity(poc_price, atr)
        levels.append(LiqLevel(
            price=poc_price, strength=2, liq_type="internal",
            source="poc", touches=1,
            note=f"{'vol-POC' if has_vol else 'range-POC'}" +
                 (f" | near {round_p:,.1f}" if is_round else "")
        ))

    # Sort: for bull → nearest is highest (closest to price from below)
    #       for bear → nearest is lowest  (closest to price from above)
    levels.sort(key=lambda x: x.price, reverse=(direction == +1))

    nearest = levels[0].price if levels else np.nan
    return levels, nearest


# ══════════════════════════════════════════════════════════════════════════════
# 4. EXTERNAL LIQUIDITY MAPPER
# ══════════════════════════════════════════════════════════════════════════════

def f_map_external_liquidity(
    df:          pd.DataFrame,
    bar_idx:     int,
    direction:   int,
    bos_level:   float,
    instrument:  str,
    lookback:    int = 200,
) -> tuple[list[LiqLevel], float, float]:
    """
    Map external liquidity levels ahead of current price — where expansion is heading.

    For BULL (direction=+1): levels ABOVE current price (pivot highs, round numbers)
    For BEAR (direction=-1): levels BELOW current price (pivot lows, round numbers)

    Note: BOS level is the 'floor' for bull and 'ceiling' for bear — we only
    include external targets that give at least 1 ATR clearance above price (bull)
    or below price (bear).

    Returns (levels_list, nearest_external, htf_target).
      nearest_external = closest level ahead of price (TP1)
      htf_target       = furthest significant level in lookback (TP2)
    """
    n       = len(df)
    start   = max(0, bar_idx - lookback)
    end_i   = bar_idx

    close   = float(df["close"].iloc[bar_idx])
    atr     = max(float(df["atr14"].iloc[bar_idx]), 1e-6)
    highs   = df["high"].values[start:end_i + 1]
    lows    = df["low"].values[start:end_i + 1]
    tol     = atr * 0.5

    # Minimum clearance: targets must be at least 1 ATR ahead of price
    if direction == +1:
        min_ext = close + atr
        max_ext = close + 40.0 * atr   # cap at 40 ATR so we don't get absurd levels
    else:
        max_ext = close - atr
        min_ext = close - 40.0 * atr

    raw_levels: list[float] = []

    # ── Prior swing pivots in lookback ────────────────────────────────────
    if direction == +1:
        piv = _scan_pivots(highs, left=8, right=3)
        for v in piv:
            if not np.isnan(v) and min_ext <= v <= max_ext:
                raw_levels.append(float(v))
        pph = float(df["prior_pivot_high"].values[bar_idx]) if "prior_pivot_high" in df.columns else np.nan
        if not np.isnan(pph) and min_ext <= pph <= max_ext:
            raw_levels.append(pph)
        # Rolling 50-bar high and 100-bar high as reference anchors
        for lb in (50, 100, 200):
            s2 = max(0, end_i - lb)
            slice_hi = df["high"].values[s2:end_i]
            if len(slice_hi) > 0:
                rh = float(np.max(slice_hi))
                if min_ext <= rh <= max_ext:
                    raw_levels.append(rh)
    else:
        piv = _scan_pivot_lows(lows, left=8, right=3)
        for v in piv:
            if not np.isnan(v) and min_ext <= v <= max_ext:
                raw_levels.append(float(v))
        ppl = float(df["prior_pivot_low"].values[bar_idx]) if "prior_pivot_low" in df.columns else np.nan
        if not np.isnan(ppl) and min_ext <= ppl <= max_ext:
            raw_levels.append(ppl)
        for lb in (50, 100, 200):
            s2 = max(0, end_i - lb)
            slice_lo = df["low"].values[s2:end_i]
            if len(slice_lo) > 0:
                rl = float(np.min(slice_lo))
                if min_ext <= rl <= max_ext:
                    raw_levels.append(rl)

    # ── Round number clusters ahead of price ──────────────────────────────
    if direction == +1:
        scan_range = np.arange(close + atr, close + 35 * atr, atr * 0.4)
    else:
        scan_range = np.arange(close - atr, close - 35 * atr, -atr * 0.4)
    for p in scan_range:
        is_round, rp = _round_number_proximity(float(p), atr)
        if is_round:
            if direction == +1 and min_ext <= rp <= max_ext:
                raw_levels.append(rp)
            elif direction == -1 and min_ext <= rp <= max_ext:
                raw_levels.append(rp)

    # ── Cluster ────────────────────────────────────────────────────────────
    clustered = _cluster_levels(raw_levels, tol)

    # ── Build LiqLevel objects ─────────────────────────────────────────────
    levels: list[LiqLevel] = []
    for price, touches in clustered:
        # Directional filter
        if direction == +1 and price < min_ext:
            continue
        if direction == -1 and price > max_ext:
            continue
        is_round, round_p = _round_number_proximity(price, atr)
        strength  = min(3, (1 if touches >= 2 else 0) + (1 if is_round else 0) +
                           (1 if touches >= 3 else 0))
        if is_round:
            src = "round_number"
        elif direction == +1:
            src = "prior_pivot" if touches >= 2 else "swing_high"
        else:
            src = "prior_pivot" if touches >= 2 else "swing_low"
        note = (f"× {touches}" if touches >= 2 else "") + \
               (f" | R{round_p:,.0f}" if is_round else "")
        levels.append(LiqLevel(price=price, strength=strength, liq_type="external",
                                source=src, touches=touches, note=note))

    # Sort: for bull → ascending (nearest above price first)
    #       for bear → descending (nearest below price first)
    levels.sort(key=lambda x: x.price, reverse=(direction == -1))

    # nearest = first TP target; htf = highest strength or most distant
    nearest = levels[0].price if levels else (close + 5 * atr if direction == +1 else close - 5 * atr)

    # HTF target = highest-strength level that is further than nearest
    htf = nearest
    if len(levels) >= 2:
        for lv in levels[1:]:
            if lv.strength >= 2:
                htf = lv.price
                break
        if htf == nearest:
            htf = levels[-1].price  # fallback: furthest level

    return levels, nearest, htf


# ══════════════════════════════════════════════════════════════════════════════
# 5. FULL LIQUIDITY CONTEXT
# ══════════════════════════════════════════════════════════════════════════════

def f_get_liquidity_context(
    df:          pd.DataFrame,
    bar_idx:     int,
    direction:   int,
    bos_level:   float,
    instrument:  str,
    sl_atr_mult: float = 1.5,
    state:       str   = "PULLBACK",
) -> LiquidityMap:
    """
    Build the complete liquidity map and trade context for one bar.

    Returns LiquidityMap with:
      internal_levels  — levels between price and BOS (pullback targets)
      external_levels  — levels beyond BOS (expansion targets)
      nearest_internal — closest stop cluster below price (bull)
      nearest_external — next major level above BOS (bull)
      htf_target       — furthest significant external level
      rr_available     — R:R from entry to nearest_external with SL at struct_bos
      valid            — False if rr_available < 2.0
      narrative        — plain English summary
    """
    close    = float(df["close"].iloc[bar_idx])
    atr      = max(float(df["atr14"].iloc[bar_idx]), 1e-6)
    dt_str   = str(df["datetime_utc"].iloc[bar_idx])

    int_lev, near_int = f_map_internal_liquidity(
        df, bar_idx, direction, bos_level, instrument
    )
    ext_lev, near_ext, htf_t = f_map_external_liquidity(
        df, bar_idx, direction, bos_level, instrument
    )

    # Suggested entry: current close
    entry = close

    # SL: just beyond nearest internal level (on the opposite side from trade)
    # For BULL: SL below nearest_internal (stop cluster); fallback = sl_atr_mult ATR below price
    # For BEAR: SL above nearest_internal; fallback = sl_atr_mult ATR above price
    if direction == +1:
        if not np.isnan(near_int):
            sl = near_int - atr * 0.3
        else:
            sl = close - sl_atr_mult * atr
        # Cap: SL cannot be closer than 0.5 ATR to entry
        sl = min(sl, close - 0.5 * atr)
    else:
        if not np.isnan(near_int):
            sl = near_int + atr * 0.3
        else:
            sl = close + sl_atr_mult * atr
        sl = max(sl, close + 0.5 * atr)

    tp1 = near_ext
    tp2 = htf_t

    rr1 = f_calculate_rr(entry, sl, tp1) if not (tp1 is None or np.isnan(tp1)) else 0.0
    rr2 = f_calculate_rr(entry, sl, tp2) if not (tp2 is None or np.isnan(tp2)) else 0.0
    rr_avail = max(rr1, rr2)
    valid    = rr_avail >= 2.0

    # ── Narrative ──────────────────────────────────────────────────────────
    def p(v: float) -> str:
        return f"{v:,.2f}" if not np.isnan(v) else "n/a"

    dir_str = "Bullish" if direction == +1 else "Bearish"
    narr    = (
        f"{instrument} {dir_str} liquidity map. "
        f"BOS at {p(bos_level)}. "
        f"Price: {p(close)}. "
    )
    if not np.isnan(near_int):
        narr += (f"Nearest internal liquidity: {p(near_int)} "
                 f"({'below' if direction == +1 else 'above'} price — pullback target). ")
    if not np.isnan(near_ext):
        narr += (f"Nearest external target (TP1): {p(near_ext)}. ")
    if not np.isnan(htf_t) and htf_t != near_ext:
        narr += f"HTF target (TP2): {p(htf_t)}. "
    narr += f"Best R:R available: {rr_avail:.1f}. "
    if not valid:
        narr += "⚠ R:R < 2.0 — do NOT enter."
    else:
        narr += f"✓ Valid setup."

    lm = LiquidityMap(
        instrument       = instrument,
        bar_idx          = bar_idx,
        datetime_utc     = dt_str[:16],
        direction        = direction,
        close            = close,
        bos_level        = bos_level,
        atr              = round(atr, 5),
        internal_levels  = int_lev,
        external_levels  = ext_lev,
        nearest_internal = near_int,
        nearest_external = near_ext,
        htf_target       = htf_t,
        rr_available     = rr_avail,
        valid            = valid,
        narrative        = narr,
        suggested_sl     = round(sl, 5),
        suggested_tp1    = round(tp1, 5) if not np.isnan(tp1) else np.nan,
        suggested_tp2    = round(tp2, 5) if not np.isnan(tp2) else np.nan,
    )
    return lm


# ══════════════════════════════════════════════════════════════════════════════
# 6. PRINT HELPER
# ══════════════════════════════════════════════════════════════════════════════

def print_liquidity_map(lm: LiquidityMap, max_levels: int = 8) -> None:
    """Pretty-print a LiquidityMap to stdout."""
    p   = lambda v: f"{v:>14,.4f}" if not (v is None or np.isnan(v)) else f"{'n/a':>14}"
    rr  = lambda v: f"{v:.1f}" if not (v is None or np.isnan(v)) else "n/a"
    drs = "BULL ↑" if lm.direction == +1 else "BEAR ↓"

    print(f"\n  {'═'*72}")
    print(f"  LIQUIDITY MAP — {lm.instrument}  {lm.datetime_utc}  {drs}")
    print(f"  {'─'*72}")
    print(f"  Close:      {p(lm.close)}   BOS level: {p(lm.bos_level)}")
    print(f"  ATR:        {p(lm.atr)}   SL (suggested): {p(lm.suggested_sl)}")
    print(f"  TP1 (near): {p(lm.suggested_tp1)}   TP2 (HTF):  {p(lm.suggested_tp2)}")
    rr_tp1 = f_calculate_rr(lm.close, lm.suggested_sl, lm.suggested_tp1) if not (lm.suggested_tp1 is None or np.isnan(lm.suggested_tp1)) else 0.0
    rr_tp2 = f_calculate_rr(lm.close, lm.suggested_sl, lm.suggested_tp2) if not (lm.suggested_tp2 is None or np.isnan(lm.suggested_tp2)) else 0.0
    print(f"  R:R TP1:{rr_tp1:.1f}  TP2:{rr_tp2:.1f}  Best:{rr(lm.rr_available)}:1   Valid: {'✓ YES' if lm.valid else '✗ NO (RR<2)'}")

    print(f"\n  EXTERNAL LIQUIDITY  (expansion targets — {len(lm.external_levels)} levels)")
    if lm.external_levels:
        for lv in lm.external_levels[:max_levels]:
            print(repr(lv))
        if len(lm.external_levels) > max_levels:
            print(f"  ... and {len(lm.external_levels) - max_levels} more")
    else:
        print("  (none found in lookback)")

    print(f"\n  ── current price: {lm.close:>12,.4f} ──")

    print(f"\n  INTERNAL LIQUIDITY  (pullback targets — {len(lm.internal_levels)} levels)")
    if lm.internal_levels:
        for lv in lm.internal_levels[:max_levels]:
            print(repr(lv))
        if len(lm.internal_levels) > max_levels:
            print(f"  ... and {len(lm.internal_levels) - max_levels} more")
    else:
        print("  (none found in lookback)")

    print(f"\n  NARRATIVE:")
    words = lm.narrative.split()
    line  = "  │  "
    for w in words:
        if len(line) + len(w) + 1 > 76:
            print(line); line = "  │  " + w + " "
        else:
            line += w + " "
    if line.strip(): print(line)
    print(f"  {'═'*72}")


# ══════════════════════════════════════════════════════════════════════════════
# 7. DIAGNOSTIC — 3 SAMPLE BARS
# ══════════════════════════════════════════════════════════════════════════════

def _run_sample_bars() -> None:
    """Run and print liquidity maps for the 3 specified XAUUSD bars."""
    from golden_highway_engine import build_features
    from macro_structure_engine import run_macro_state_machine

    inst = "XAUUSD"
    tf   = "1H"
    print(f"\n{'═'*74}")
    print(f"  LIQUIDITY MAPPER — {inst} {tf} — 3 SAMPLE BARS")
    print(f"{'═'*74}")

    df = build_features(inst, tf)
    if df is None:
        print("  No data"); return

    # Run macro state machine to get BOS levels, directions, states
    states = run_macro_state_machine(df, inst, tf)
    state_by_bar = {r["bar"]: r for r in states}

    # The 3 target bars
    targets = [
        (1418, "LATE_EXPANSION  — what liquidity existed above/below?"),
        (2786, "MID_PULLBACK    — where was the nearest sweep target?"),
        (13450, "LIQ_SWEEP       — what was swept? What RR after?"),
    ]

    for bar_idx, label in targets:
        sr = state_by_bar.get(bar_idx)
        if sr is None:
            print(f"\n  Bar {bar_idx} not found in state results"); continue

        direction = sr["macro_dir"]
        bos_level = sr["bos_level"] or (df["close"].iloc[bar_idx])
        state     = sr["state"]
        location  = sr["location"]

        print(f"\n  ┌─ Bar {bar_idx}  {sr['datetime_utc'][:16]}  "
              f"State={state}  Loc={location}")
        print(f"  │  {label}")

        lm = f_get_liquidity_context(
            df         = df,
            bar_idx    = bar_idx,
            direction  = direction,
            bos_level  = bos_level,
            instrument = inst,
        )
        print_liquidity_map(lm)

        # Additional plain-English for each bar
        close = df["close"].iloc[bar_idx]
        atr   = df["atr14"].iloc[bar_idx]
        print(f"\n  PLAIN ENGLISH SUMMARY — Bar {bar_idx}:")
        if bar_idx == 1418:
            # Late expansion
            near_i = lm.nearest_internal
            near_e = lm.nearest_external
            print(f"  Price at {close:,.2f}, in LATE EXPANSION. BOS (key support): {bos_level:,.2f}.")
            print(f"  External target (where expansion is heading): {near_e:,.2f}.")
            print(f"  Internal liquidity below (pullback target if price reverses): {near_i:,.2f}.")
            print(f"  R:R available = {lm.rr_available:.1f}. {'Trade valid.' if lm.valid else 'R:R too low — do not trade.'}")
            print(f"  ACTION: Do NOT add new longs here — late expansion. Wait for pullback to")
            print(f"  {near_i:,.2f} before re-entering.")

        elif bar_idx == 2786:
            near_i = lm.nearest_internal
            near_e = lm.nearest_external
            print(f"  Price at {close:,.2f}, in MID PULLBACK. BOS (support): {bos_level:,.2f}.")
            print(f"  Nearest internal liquidity (sweep target): {near_i:,.2f}.")
            print(f"  Once internal is swept → external expansion target: {near_e:,.2f}.")
            print(f"  R:R from near internal level = {lm.rr_available:.1f}.")
            print(f"  ACTION: Wait for price to reach {near_i:,.2f} and show reversal.")
            print(f"  That sweep level is the entry trigger zone.")

        elif bar_idx == 13450:
            near_i = lm.nearest_internal
            near_e = lm.nearest_external
            print(f"  Price at {close:,.2f}, LIQ_SWEEP event. BOS: {bos_level:,.2f}.")
            print(f"  Level swept: fractal near {near_i:,.2f} (price pierced then recovered).")
            print(f"  After sweep: watch for micro BOS above {bos_level:,.2f}.")
            print(f"  External target after micro BOS confirms: {near_e:,.2f}.")
            print(f"  R:R = {lm.rr_available:.1f}. {'Valid entry opportunity.' if lm.valid else 'R:R insufficient.'}")


if __name__ == "__main__":
    _run_sample_bars()
