"""
research/pullback_engine.py
============================
Golden Highway Architecture v3 — Pullback Completion Scorer

Scores how COMPLETE a pullback is on a 0–15 scale using 7 checks.
The higher the score, the more evidence that the pullback is exhausted
and the next expansion leg is imminent.

7 Checks (max score in brackets):

  Check 1 — Micro RSI position (0–3):
    Score 3: micro >= 80  (RSI has fallen 48+ pts from peak → deep pullback)
    Score 2: micro >= 60
    Score 1: micro >= 40
    Score 0: micro < 40  (pullback shallow or not started)

  Check 2 — Location within pullback (0–3):
    PULLBACK_COMPLETE or LATE_PULLBACK  → 3
    MID_PULLBACK                        → 2
    EARLY_PULLBACK                      → 1
    anything else                       → 0

  Check 3 — Price at key internal liquidity level (0–2):
    Within 0.5 ATR of a 3-star internal level → 2
    Within 0.5 ATR of a 2-star level           → 1
    Not near any internal level                → 0

  Check 4 — Volume dry-up (0–2):
    For instruments with volume: RVOL < 0.75 on this bar → 2
                                 RVOL < 0.90             → 1
                                 RVOL >= 0.90            → 0
    For no-volume instruments: ATR contraction proxy (current ATR < 0.85 * 20-bar avg ATR)
                                 → 2 if < 0.75, 1 if < 0.85, 0 otherwise

  Check 5 — LTF fractal forming (0–2):
    Fractal low printing (bull) or fractal high (bear) at current price ± 1 ATR → 2
    Prior bar had fractal                                                          → 1
    No fractal                                                                     → 0

  Check 6 — HTF alignment holding (0–2):
    4H + 1H both aligned with trade direction (htf_align == +1 for bull) → 2
    4H aligned, 1H mixed                                                   → 1
    Neither aligned                                                        → 0

  Check 7 — Flow score turning (0–1):
    flow_score recovering from low (current flow > prior_flow by ≥ 1) → 1
    Still declining                                                      → 0

TOTAL: max 15. Interpretation:
    13–15  PULLBACK_COMPLETE — high confidence, ready to enter on next BOS
    10–12  LATE_PULLBACK     — evidence strong, may need 1 more bar
    7–9    MID_PULLBACK      — incomplete, wait
    4–6    EARLY_PULLBACK    — do not trade yet
    0–3    NO_PULLBACK       — not in pullback, state machine may be wrong

Column map (from build_features):
    micro, flow_score, vol_ratio, atr14, htf_align, session
    last_pivot_low, last_pivot_high (for fractal detection)
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
from liquidity_mapper  import f_map_internal_liquidity


# ══════════════════════════════════════════════════════════════════════════════
# 0. MOMENTUM BUILDING
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class MomentumBuild:
    """Result of f_momentum_building() — describes score acceleration."""
    building:        bool   = False
    bars_building:   int    = 0      # consecutive bars with increasing score
    acceleration:    float  = 0.0    # average score increase per bar
    score_sequence:  list   = field(default_factory=list)  # e.g. [8, 10, 11, 13]
    entry_bonus:     int    = 0      # +2 if building, else 0
    detail:          str    = ""

    def __repr__(self) -> str:
        if not self.building:
            return f"MomentumBuild(building=False)"
        seq = " → ".join(str(s) for s in self.score_sequence)
        return (f"MomentumBuild(building=True, bars={self.bars_building}, "
                f"accel=+{self.acceleration:.1f}/bar, seq=[{seq}], bonus=+{self.entry_bonus})")


def f_momentum_building(scores_last_5: list) -> MomentumBuild:
    """
    Detect whether pullback scores are accelerating upward over recent bars.

    Accepts a list of PullbackScore objects (or ints) ordered oldest→newest.
    Looks for the longest consecutive run of INCREASING scores at the tail.

    Rules:
      - Minimum 3 consecutive increasing bars required to set building=True
      - Each bar must score strictly HIGHER than the previous
      - acceleration = (last_score - first_score) / (n_bars - 1)
      - entry_bonus = +2 if building else 0

    Parameters
    ----------
    scores_last_5 : list of PullbackScore or int
        Last N pullback scores for the same pullback episode (oldest first).
        Typically pass the last 5 bars for the current instrument.

    Returns
    -------
    MomentumBuild
    """
    if not scores_last_5:
        return MomentumBuild()

    # Extract raw totals
    totals = []
    for s in scores_last_5:
        if isinstance(s, int):
            totals.append(s)
        elif hasattr(s, "total"):
            totals.append(s.total)
        else:
            try:
                totals.append(int(s))
            except Exception:
                pass

    if len(totals) < 2:
        return MomentumBuild()

    # Find longest consecutive increasing run ending at the LAST bar
    run = [totals[-1]]
    for v in reversed(totals[:-1]):
        if v < run[0]:          # strictly less than first in current run
            run.insert(0, v)
        else:
            break               # chain broken

    if len(run) < 3:
        return MomentumBuild(
            building=False,
            score_sequence=run,
            detail=f"Only {len(run)} increasing bars (need ≥ 3)"
        )

    n_bars       = len(run)
    acceleration = (run[-1] - run[0]) / max(n_bars - 1, 1)
    detail       = (f"{n_bars} consecutive increasing bars: "
                    f"{' → '.join(str(v) for v in run)}, "
                    f"acceleration +{acceleration:.1f}/bar")

    return MomentumBuild(
        building      = True,
        bars_building = n_bars,
        acceleration  = round(acceleration, 2),
        score_sequence= run,
        entry_bonus   = 2,
        detail        = detail,
    )


# ══════════════════════════════════════════════════════════════════════════════
# 1. RESULT DATACLASS
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class PullbackScore:
    bar_idx:        int
    datetime_utc:   str
    instrument:     str
    state:          str
    location:       str
    direction:      int      # +1 bull / -1 bear
    close:          float
    atr:            float

    # Individual check scores
    c1_micro:       int      # 0–3
    c2_location:    int      # 0–3
    c3_liq_level:   int      # 0–2
    c4_vol_dryup:   int      # 0–2
    c5_fractal:     int      # 0–2
    c6_htf_align:   int      # 0–2
    c7_flow_turn:   int      # 0–1

    total:          int              # 0–15
    grade:          str              # PULLBACK_COMPLETE / LATE / MID / EARLY / NONE

    # Momentum building (populated by batch scorer or caller)
    momentum:       MomentumBuild = field(default_factory=MomentumBuild)

    # Detail strings
    c1_detail:      str = ""
    c2_detail:      str = ""
    c3_detail:      str = ""
    c4_detail:      str = ""
    c5_detail:      str = ""
    c6_detail:      str = ""
    c7_detail:      str = ""

    def __repr__(self) -> str:
        bar_str  = f"│ Bar {self.bar_idx:>5}  {self.datetime_utc[:16]}  "
        dir_str  = "BULL" if self.direction == +1 else "BEAR"
        state_str = f"{self.state[:9]:<9}/{self.location[:14]:<14}"
        score_str = f"TOTAL={self.total:>2}/15  [{self.grade}]"
        checks    = (f"C1={self.c1_micro} C2={self.c2_location} "
                     f"C3={self.c3_liq_level} C4={self.c4_vol_dryup} "
                     f"C5={self.c5_fractal} C6={self.c6_htf_align} "
                     f"C7={self.c7_flow_turn}")
        return (f"{bar_str}{dir_str} {state_str}  "
                f"{score_str}  {checks}")


# ══════════════════════════════════════════════════════════════════════════════
# 2. GRADE HELPER
# ══════════════════════════════════════════════════════════════════════════════

def _grade(total: int) -> str:
    if total >= 13: return "PULLBACK_COMPLETE"
    if total >= 10: return "LATE_PULLBACK"
    if total >= 7:  return "MID_PULLBACK"
    if total >= 4:  return "EARLY_PULLBACK"
    return "NO_PULLBACK"


# ══════════════════════════════════════════════════════════════════════════════
# 3. CORE SCORER
# ══════════════════════════════════════════════════════════════════════════════

def f_score_pullback(
    df:          pd.DataFrame,
    bar_idx:     int,
    direction:   int,
    bos_level:   float,
    state:       str,
    location:    str,
    instrument:  str,
) -> PullbackScore:
    """
    Score pullback completion for a single bar.

    df must have columns: micro, flow_score, vol_ratio, atr14, htf_align,
                          last_pivot_low, last_pivot_high, prior_pivot_low,
                          prior_pivot_high, close, high, low

    Parameters
    ----------
    direction : +1 (bull pullback — price pulling back in a bull trend)
                -1 (bear pullback — price pulling back in a bear trend)
    bos_level : most recent structural BOS level
    state     : macro state string (from macro_structure_engine)
    location  : location label (from macro_structure_engine)
    """
    row      = df.iloc[bar_idx]
    close    = float(row["close"])
    atr      = max(float(row.get("atr14", 1.0)), 1e-6)
    has_vol  = f_has_volume(instrument)
    dt_str   = str(row.get("datetime_utc", f"bar_{bar_idx}"))

    # ── Safely get columns ────────────────────────────────────────────────
    def get(col: str, default: float = np.nan) -> float:
        v = row.get(col, default)
        return float(v) if v is not None and not (isinstance(v, float) and np.isnan(v)) else default

    micro      = get("micro",      0.0)
    flow       = get("flow_score", 0.0)
    vol_ratio  = get("vol_ratio",  1.0)
    htf_align  = get("htf_align",  0.0)

    # Flow at prior bar (for check 7)
    if bar_idx > 0:
        prior_flow = float(df.iloc[bar_idx - 1].get("flow_score", flow))
    else:
        prior_flow = flow

    # ── Check 1: Micro RSI position ───────────────────────────────────────
    # micro column = RSI cycle completion index 0–100
    # High micro = deep pullback = closer to complete
    if micro >= 80:
        c1 = 3; c1_d = f"micro={micro:.0f} ≥ 80 — deep pullback (RSI fell 48+ pts)"
    elif micro >= 60:
        c1 = 2; c1_d = f"micro={micro:.0f} ≥ 60 — moderate pullback"
    elif micro >= 40:
        c1 = 1; c1_d = f"micro={micro:.0f} ≥ 40 — early pullback forming"
    else:
        c1 = 0; c1_d = f"micro={micro:.0f} < 40 — pullback not yet started"

    # ── Check 2: Location within pullback ────────────────────────────────
    if location in ("PULLBACK_COMPLETE",):
        c2 = 3; c2_d = f"location={location} — complete"
    elif location in ("LATE_PULLBACK",):
        c2 = 3; c2_d = f"location={location} — late, high quality zone"
    elif location in ("MID_PULLBACK",):
        c2 = 2; c2_d = f"location={location} — mid, still valid"
    elif location in ("EARLY_PULLBACK",):
        c2 = 1; c2_d = f"location={location} — early, wait"
    elif location in ("SWEEP_AT_LOW", "SWEEP_AT_HIGH"):
        c2 = 3; c2_d = f"location={location} — sweep event, high conviction"
    elif location in ("REACCUM_CONFIRMED",):
        c2 = 3; c2_d = f"location={location} — reaccumulation confirmed"
    else:
        c2 = 0; c2_d = f"location={location} — not a pullback location"

    # ── Check 3: Price at key internal liquidity level ───────────────────
    try:
        int_lev, near_int = f_map_internal_liquidity(
            df, bar_idx, direction, bos_level, instrument, lookback=60
        )
        c3    = 0
        c3_d  = "No internal level within 0.5 ATR"
        for lv in int_lev:
            if abs(close - lv.price) <= 0.5 * atr:
                if lv.strength >= 3:
                    c3 = 2; c3_d = f"At {lv.price:.2f} ({lv.source}, ★★★)"
                    break
                elif lv.strength >= 2 and c3 < 2:
                    c3 = 1; c3_d = f"Near {lv.price:.2f} ({lv.source}, ★★)"
    except Exception as e:
        c3 = 0; c3_d = f"liq mapper error: {e}"

    # ── Check 4: Volume dry-up ────────────────────────────────────────────
    if has_vol:
        if vol_ratio <= 0.75:
            c4 = 2; c4_d = f"RVOL={vol_ratio:.2f} ≤ 0.75 — clear volume dry-up"
        elif vol_ratio <= 0.90:
            c4 = 1; c4_d = f"RVOL={vol_ratio:.2f} ≤ 0.90 — declining volume"
        else:
            c4 = 0; c4_d = f"RVOL={vol_ratio:.2f} — volume still elevated"
    else:
        # No-volume proxy: ATR contraction
        aavg = 0.0
        if bar_idx >= 20:
            atrs = df["atr14"].values[bar_idx - 20: bar_idx]
            aavg = float(np.nanmean(atrs))
        if aavg > 0 and atr <= 0.75 * aavg:
            c4 = 2; c4_d = f"ATR={atr:.2f} ≤ 0.75*avg={aavg:.2f} — range compression (no-vol proxy)"
        elif aavg > 0 and atr <= 0.85 * aavg:
            c4 = 1; c4_d = f"ATR={atr:.2f} ≤ 0.85*avg={aavg:.2f} — mild compression"
        else:
            c4 = 0; c4_d = f"ATR={atr:.2f} — no compression (no-vol proxy)"

    # ── Check 5: LTF fractal forming at current price ────────────────────
    # We use last_pivot_low (bull) and last_pivot_high (bear) from dataframe
    # A fractal is "forming" if the pivot is within 1 ATR of current price
    c5   = 0
    c5_d = "No fractal within 1 ATR"
    try:
        if direction == +1:
            piv_col = "last_pivot_low"
        else:
            piv_col = "last_pivot_high"

        piv_now  = float(df[piv_col].iloc[bar_idx]) if piv_col in df.columns else np.nan
        piv_prev = float(df[piv_col].iloc[bar_idx - 1]) if bar_idx > 0 and piv_col in df.columns else np.nan

        if not np.isnan(piv_now) and abs(close - piv_now) <= 1.0 * atr:
            c5 = 2; c5_d = f"Fractal {direction and 'low' or 'high'} at {piv_now:.2f} (current bar)"
        elif not np.isnan(piv_prev) and abs(close - piv_prev) <= 1.5 * atr:
            c5 = 1; c5_d = f"Fractal from prior bar at {piv_prev:.2f}"
        else:
            # Scan manually for recent fractal (last 5 bars)
            if piv_col in df.columns:
                recent_pivs = df[piv_col].values[max(0, bar_idx - 5): bar_idx]
                valid_pivs  = [v for v in recent_pivs if not np.isnan(v)]
                if valid_pivs:
                    nearest_piv = min(valid_pivs, key=lambda v: abs(close - v))
                    if abs(close - nearest_piv) <= 1.5 * atr:
                        c5 = 1; c5_d = f"Recent fractal at {nearest_piv:.2f} (within 5 bars)"
    except Exception as e:
        c5_d = f"fractal check error: {e}"

    # ── Check 6: HTF alignment ────────────────────────────────────────────
    # htf_align: +1 = 4H and 1H both bullish, -1 = both bearish, 0 = mixed
    htf_target = +1 if direction == +1 else -1
    if htf_align == htf_target:
        c6 = 2; c6_d = f"htf_align={htf_align:.0f} — 4H + 1H aligned with direction"
    elif abs(htf_align) == 0:
        c6 = 1; c6_d = f"htf_align={htf_align:.0f} — HTF mixed (partial credit)"
    else:
        c6 = 0; c6_d = f"htf_align={htf_align:.0f} — HTF opposing direction"

    # ── Check 7: Flow score turning ───────────────────────────────────────
    # For bull: flow_score was negative (pullback) and is now recovering (rising)
    # For bear: flow_score was positive and is now declining (turning more negative)
    if direction == +1:
        flow_recovering = flow > prior_flow + 0.5  # turning more bullish
        c7   = 1 if flow_recovering else 0
        c7_d = (f"flow {prior_flow:.1f} → {flow:.1f} ↑ turning bullish"
                if flow_recovering
                else f"flow {prior_flow:.1f} → {flow:.1f} — not yet turning")
    else:
        flow_recovering = flow < prior_flow - 0.5  # turning more bearish
        c7   = 1 if flow_recovering else 0
        c7_d = (f"flow {prior_flow:.1f} → {flow:.1f} ↓ turning bearish"
                if flow_recovering
                else f"flow {prior_flow:.1f} → {flow:.1f} — not yet turning")

    total = c1 + c2 + c3 + c4 + c5 + c6 + c7
    grade = _grade(total)

    return PullbackScore(
        bar_idx      = bar_idx,
        datetime_utc = dt_str[:16],
        instrument   = instrument,
        state        = state,
        location     = location,
        direction    = direction,
        close        = close,
        atr          = round(atr, 5),
        c1_micro     = c1,
        c2_location  = c2,
        c3_liq_level = c3,
        c4_vol_dryup = c4,
        c5_fractal   = c5,
        c6_htf_align = c6,
        c7_flow_turn = c7,
        total        = total,
        grade        = grade,
        c1_detail    = c1_d,
        c2_detail    = c2_d,
        c3_detail    = c3_d,
        c4_detail    = c4_d,
        c5_detail    = c5_d,
        c6_detail    = c6_d,
        c7_detail    = c7_d,
    )


# ══════════════════════════════════════════════════════════════════════════════
# 4. BATCH SCORER
# ══════════════════════════════════════════════════════════════════════════════

def f_score_pullbacks_batch(
    df:          pd.DataFrame,
    state_data:  list[dict],
    instrument:  str,
    states_filter: tuple[str, ...] = ("PULLBACK", "REACCUMULATION", "LIQ_SWEEP"),
) -> list[PullbackScore]:
    """
    Score all bars in state_data that are in the specified states.

    state_data: list of dicts from run_macro_state_machine().
    Returns list of PullbackScore sorted by bar_idx.
    """
    results = []
    for sr in state_data:
        if sr.get("state") not in states_filter:
            continue
        bar_idx   = sr["bar"]
        direction = sr.get("macro_dir", +1)
        bos_level = sr.get("bos_level") or float(df["close"].iloc[bar_idx])
        state_s   = sr.get("state", "UNKNOWN")
        location  = sr.get("location", "UNKNOWN")

        try:
            sc = f_score_pullback(
                df         = df,
                bar_idx    = bar_idx,
                direction  = direction,
                bos_level  = bos_level,
                state      = state_s,
                location   = location,
                instrument = instrument,
            )
            results.append(sc)
        except Exception:
            pass  # skip bars with missing data

    # ── Attach momentum_building to each bar ──────────────────────────────
    # Build a lookup: bar_idx → position in results list
    # For each bar, look back up to 5 prior scored bars to build the sequence
    bar_to_idx = {sc.bar_idx: i for i, sc in enumerate(results)}
    for i, sc in enumerate(results):
        # Gather up to 5 consecutive prior bars (same direction)
        window: list[PullbackScore] = []
        j = i - 1
        while j >= 0 and len(window) < 4:
            prev = results[j]
            # Only include bars in the same pullback direction
            if prev.direction == sc.direction:
                window.insert(0, prev)
            j -= 1
        window.append(sc)                       # current bar at end
        totals = [w.total for w in window]
        sc.momentum = f_momentum_building(totals)

    return results


# ══════════════════════════════════════════════════════════════════════════════
# 5. DIAGNOSTIC — 10 XAUUSD OOS PULLBACK SCORES
# ══════════════════════════════════════════════════════════════════════════════

def _run_sample_scores() -> None:
    from golden_highway_engine import build_features
    from macro_structure_engine import run_macro_state_machine

    inst = "XAUUSD"
    tf   = "1H"

    print(f"\n{'═'*100}")
    print(f"  PULLBACK COMPLETION SCORES — {inst} {tf} — 10 BARS FROM OOS WINDOW")
    print(f"{'═'*100}")

    df = build_features(inst, tf)
    if df is None:
        print("  No data"); return

    n_total = len(df)
    oos_start = int(n_total * 0.80)

    print(f"  Total bars: {n_total}  |  OOS window: bar {oos_start} → {n_total - 1}  "
          f"({n_total - oos_start} bars)")

    states = run_macro_state_machine(df, inst, tf)
    # Filter to OOS window
    oos_states = [s for s in states if s["bar"] >= oos_start]

    scores = f_score_pullbacks_batch(df, oos_states, inst)
    print(f"  Pullback/Reaccum/Sweep bars in OOS: {len(scores)}")

    if not scores:
        print("  No pullback bars found in OOS window"); return

    # Show top 10 by total score (highest first)
    top10 = sorted(scores, key=lambda x: x.total, reverse=True)[:10]

    print(f"\n  TOP 10 PULLBACK SCORES (sorted by total, desc):\n")
    print(f"  {'─'*100}")
    header = (f"  {'Bar':>5}  {'Date':^16}  {'Dir':4}  "
              f"{'State':^9}  {'Location':^16}  {'Close':>10}  "
              f"{'C1':>2} {'C2':>2} {'C3':>2} {'C4':>2} {'C5':>2} {'C6':>2} {'C7':>2}  "
              f"{'TOT':>3}  {'Grade'}")
    print(header)
    print(f"  {'─'*100}")

    for sc in top10:
        dir_s  = "BULL" if sc.direction == +1 else "BEAR"
        state9 = sc.state[:9]
        loc16  = sc.location[:16]
        print(f"  {sc.bar_idx:>5}  {sc.datetime_utc[:16]:^16}  {dir_s:4}  "
              f"{state9:^9}  {loc16:^16}  {sc.close:>10,.2f}  "
              f"{sc.c1_micro:>2} {sc.c2_location:>2} {sc.c3_liq_level:>2} "
              f"{sc.c4_vol_dryup:>2} {sc.c5_fractal:>2} {sc.c6_htf_align:>2} "
              f"{sc.c7_flow_turn:>2}  {sc.total:>3}  {sc.grade}")

    print(f"  {'─'*100}")

    # Detailed breakdown of the #1 bar
    best = top10[0]
    print(f"\n  DETAILED BREAKDOWN — Bar {best.bar_idx} (score {best.total}/15 — {best.grade})")
    print(f"  {'─'*60}")
    print(f"  C1 Micro RSI  [{best.c1_micro}/3]:  {best.c1_detail}")
    print(f"  C2 Location   [{best.c2_location}/3]:  {best.c2_detail}")
    print(f"  C3 Liq Level  [{best.c3_liq_level}/2]:  {best.c3_detail}")
    print(f"  C4 Vol Dry-up [{best.c4_vol_dryup}/2]:  {best.c4_detail}")
    print(f"  C5 Fractal    [{best.c5_fractal}/2]:  {best.c5_detail}")
    print(f"  C6 HTF Align  [{best.c6_htf_align}/2]:  {best.c6_detail}")
    print(f"  C7 Flow Turn  [{best.c7_flow_turn}/1]:  {best.c7_detail}")

    # Score distribution
    print(f"\n  SCORE DISTRIBUTION (all {len(scores)} pullback bars in OOS):")
    grade_counts: dict[str, int] = {}
    for sc in scores:
        grade_counts[sc.grade] = grade_counts.get(sc.grade, 0) + 1
    order = ["PULLBACK_COMPLETE", "LATE_PULLBACK", "MID_PULLBACK", "EARLY_PULLBACK", "NO_PULLBACK"]
    for g in order:
        cnt = grade_counts.get(g, 0)
        pct = cnt / len(scores) * 100
        bar = "█" * int(pct / 2)
        print(f"  {g:<20} {cnt:>4}  {pct:>5.1f}%  {bar}")


if __name__ == "__main__":
    _run_sample_scores()
