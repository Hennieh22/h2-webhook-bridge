"""
research/entry_score.py
========================
Golden Highway Architecture v3 — Unified Entry Score (0–100)

5 layers × 20 points each = 100 total.

  Layer 1 — Macro Structure State (0–20)
  Layer 2 — Location Quality + Session (0–20)
  Layer 3 — Liquidity Map Quality (0–20)
  Layer 4 — Markov Continuation (0–20)
  Layer 5 — LTF Pullback Score + Momentum Building (0–20, bonus +2 capped at 20)

Grading:
  A+   80–100   full size  (1.00×)
  A    65–79    0.75×
  B    50–64    0.50×
  C    35–49    0.25×
  SKIP  0–34    0×

Prop filter (ALL required for A or A+):
  total ≥ 65
  markov_prob ≥ 0.65
  pullback_total ≥ 9
  rr_available ≥ 3.0
  state NOT in (DISTRIBUTION, TREND_FAILURE)
  session valid for instrument
"""

from __future__ import annotations

import sys, re, warnings
warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd
from pathlib import Path
from dataclasses import dataclass, field

sys.path.insert(0, str(Path(__file__).resolve().parent))
from structure_engine  import f_has_volume
from liquidity_mapper  import f_get_liquidity_context, LiquidityMap
from pullback_engine   import (f_score_pullback, f_score_pullbacks_batch,
                                f_momentum_building, PullbackScore, MomentumBuild)


# ══════════════════════════════════════════════════════════════════════════════
# SESSION RULES  (v4 — session conviction calibrated from OOS analysis)
# ══════════════════════════════════════════════════════════════════════════════

FOREX = {"EURUSD","GBPUSD","USDJPY","AUDUSD","USDCAD",
         "EURJPY","GBPJPY","USDCHF","NZDUSD","EURGBP","AUDNZD","CADJPY"}

# Off-hours: any UTC hour not covered by any instrument-valid window
# Each value is a list of (start_inclusive, end_exclusive) UTC hour pairs.
_SESSION_WINDOWS = {
    "JP225":  [(0, 9), (13, 21)],        # Tokyo + NY; Overlap included
    "DE40":   [(7, 16)],  "DAX":  [(7, 16)],
    "UK100":  [(7, 16)],  "FTSE": [(7, 16)],
    "XAUUSD": [(7, 16), (13, 20)],
    "XAGUSD": [(7, 16), (13, 20)],
    "US30":   [(13, 20)], "USTEC": [(13, 20)],
    "HK50":   [(1, 9)],
}

# Per-instrument session conviction: points added to Layer 2 score.
# Calibrated from OOS trade analysis.  Others use the generic kill-zone rule.
# JP225: London/Overlap 100% WR (+4), Tokyo 57% WR (+2), NY 40% WR (-2)
_SESSION_CONVICTION: dict[str, dict[str, int]] = {
    "JP225": {
        "LONDON":   +4,   # 07–12 UTC  — 100% WR in OOS analysis
        "OVERLAP":  +4,   # 12–16 UTC  — grouped with London
        "TOKYO":    +2,   # 23–08 UTC  — 57% WR, solid but not elite
        "NY":       -2,   # 16–21 UTC  — 40% WR, caution
        "OFFHOURS": -4,   # outside all windows — block
    },
}

def _utc_session_label(utc_hour: int) -> str:
    """Classify a UTC hour into a named session."""
    if utc_hour >= 23 or utc_hour < 8:
        return "TOKYO"
    if 7 <= utc_hour < 12:
        return "LONDON"
    if 12 <= utc_hour < 16:
        return "OVERLAP"
    if 16 <= utc_hour < 21:
        return "NY"
    return "OFFHOURS"


def _session_info(instrument: str, utc_hour: int) -> tuple[bool, bool, str, int]:
    """
    Returns (valid, in_killzone, detail, conviction_pts).

    conviction_pts: score delta applied to L2 (instrument-calibrated).
      Instruments without a _SESSION_CONVICTION entry get the flat
      kill-zone bonus (+4 in KZ / +2 valid / 0 invalid).
    """
    inst    = instrument.upper()
    sess_lbl = _utc_session_label(utc_hour)

    # Instruments with calibrated conviction tables
    if inst in _SESSION_CONVICTION:
        windows = _SESSION_WINDOWS.get(inst, [])
        valid   = any(lo <= utc_hour < hi for lo, hi in windows)
        in_kz   = valid and ((7 <= utc_hour <= 9) or (13 <= utc_hour <= 15))
        conv    = _SESSION_CONVICTION[inst].get(sess_lbl, 0)
        detail  = (f"{sess_lbl} [conviction {conv:+d}] "
                   f"{'KZ ' if in_kz else ''}{utc_hour:02d}:00 UTC")
        # OFFHOURS blocks the signal (valid=False) even for JP225
        if sess_lbl == "OFFHOURS":
            return False, False, detail, conv
        return True, in_kz, detail, conv

    # Forex: always valid, kill-zone gets +4, otherwise +2
    if inst in FOREX:
        in_kz  = (7 <= utc_hour <= 9) or (13 <= utc_hour <= 15)
        conv   = 4 if in_kz else 2
        detail = f"Forex {sess_lbl} {'KZ ' if in_kz else ''}{utc_hour:02d}:00 UTC"
        return True, in_kz, detail, conv

    # Generic indexed instruments
    windows = _SESSION_WINDOWS.get(inst)
    if windows is None:
        return True, False, f"No rule — neutral {utc_hour:02d}:00 UTC", 2
    valid = any(lo <= utc_hour < hi for lo, hi in windows)
    in_kz = valid and ((7 <= utc_hour <= 9) or (13 <= utc_hour <= 15))
    if not valid:
        return False, False, f"Outside valid session {utc_hour:02d}:00 UTC", 0
    conv   = 4 if in_kz else 2
    return True, in_kz, f"{'Kill zone ' if in_kz else 'Valid '}{utc_hour:02d}:00 UTC", conv


# ══════════════════════════════════════════════════════════════════════════════
# RESULT DATACLASS
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class EntryScore:
    bar_idx:          int
    datetime_utc:     str
    instrument:       str
    state:            str
    location:         str
    direction:        int
    close:            float
    atr:              float

    l1_structure:     int        # 0–20  macro state quality
    l2_location:      int        # 0–20  location + session
    l3_liquidity:     int        # 0–20  RR + level strength
    l4_markov:        int        # 0–20  continuation probability
    l5_pullback:      int        # 0–20  pullback score + momentum bonus

    total:            int        # 0–100
    grade:            str        # A+ / A / B / C / SKIP
    size_mult:        float      # 1.0 / 0.75 / 0.5 / 0.25 / 0.0

    rr_available:     float
    pullback_total:   int
    markov_prob:      float
    session_valid:    bool
    in_killzone:      bool

    prop_filter_pass: bool
    prop_reasons:     list[str] = field(default_factory=list)

    l1_detail:        str = ""
    l2_detail:        str = ""
    l3_detail:        str = ""
    l4_detail:        str = ""
    l5_detail:        str = ""
    momentum_detail:  str = ""

    pullback_score_obj: PullbackScore = None
    liquidity_map:      LiquidityMap  = None
    momentum:           MomentumBuild = field(default_factory=MomentumBuild)


def _grade_and_size(total: int) -> tuple[str, float]:
    if total >= 80: return "A+",   1.00
    if total >= 65: return "A",    0.75
    if total >= 50: return "B",    0.50
    if total >= 35: return "C",    0.25
    return "SKIP", 0.0


# ══════════════════════════════════════════════════════════════════════════════
# LAYER SCORERS  (each returns int score + detail string)
# ══════════════════════════════════════════════════════════════════════════════

def _l1_structure(state: str) -> tuple[int, str]:
    """Layer 1: Macro state quality — 0/8/12/16/20."""
    m = {
        "PULLBACK":       (20, "PULLBACK — primary entry state"),
        "LIQ_SWEEP":      (18, "LIQ_SWEEP — high-precision reversal"),
        "REACCUMULATION": (16, "REACCUMULATION — accumulation zone confirmed"),
        "EXPANSION":      (10, "EXPANSION — only early entries valid"),
        "RANGE":          (6,  "RANGE — low directional confidence"),
        "DISTRIBUTION":   (0,  "DISTRIBUTION — avoid"),
        "TREND_FAILURE":  (0,  "TREND_FAILURE — avoid"),
    }
    score, detail = m.get(state, (4, f"{state} — unclassified"))
    return score, detail


def _l2_location(location: str, session_valid: bool, in_kz: bool,
                 session_detail: str,
                 conviction_pts: int | None = None) -> tuple[int, str]:
    """Layer 2: Location quality (0–16) + session bonus.

    If conviction_pts is supplied (instrument has a calibrated conviction
    table), it replaces the generic kill-zone bonus and can be negative.
    """
    loc_map = {
        "PULLBACK_COMPLETE": (16, "PULLBACK_COMPLETE — full retrace confirmed"),
        "LATE_PULLBACK":     (15, "LATE_PULLBACK — 66–82% retrace, prime zone"),
        "MID_PULLBACK":      (10, "MID_PULLBACK — 33–66% retrace, wait"),
        "EARLY_PULLBACK":    (5,  "EARLY_PULLBACK — < 33%, too early"),
        "SWEEP_AT_LOW":      (15, "SWEEP_AT_LOW — liquidity swept, reversal setup"),
        "SWEEP_AT_HIGH":     (15, "SWEEP_AT_HIGH — liquidity swept, reversal setup"),
        "REACCUM_CONFIRMED": (14, "REACCUM_CONFIRMED — accumulation complete"),
        "EARLY_EXPANSION":   (10, "EARLY_EXPANSION — adding to winner"),
        "MID_EXPANSION":     (5,  "MID_EXPANSION — late to the move"),
        "LATE_EXPANSION":    (1,  "LATE_EXPANSION — do not enter"),
    }
    loc_score, loc_detail = loc_map.get(location, (4, f"{location}"))

    if conviction_pts is not None:
        # Calibrated instrument: use conviction directly (can be negative)
        sess_bonus = conviction_pts if session_valid else 0
    elif not session_valid:
        sess_bonus = 0
    elif in_kz:
        sess_bonus = 4
    else:
        sess_bonus = 2

    total  = max(0, min(loc_score + sess_bonus, 20))
    detail = (f"{loc_detail} | Session: {session_detail} "
              f"[{sess_bonus:+d} pts]")
    return total, detail


def _l3_liquidity(lm: LiquidityMap) -> tuple[int, str]:
    """Layer 3: R:R + level strength (0–20)."""
    rr = lm.rr_available if not np.isnan(lm.rr_available) else 0.0

    # R:R component: 0–10
    if rr >= 5.0:   rr_pts = 10; rr_d = f"RR={rr:.1f}≥5 (+10)"
    elif rr >= 4.0: rr_pts = 8;  rr_d = f"RR={rr:.1f}≥4 (+8)"
    elif rr >= 3.0: rr_pts = 6;  rr_d = f"RR={rr:.1f}≥3 (+6)"
    elif rr >= 2.0: rr_pts = 3;  rr_d = f"RR={rr:.1f}≥2 (+3)"
    else:           rr_pts = 0;  rr_d = f"RR={rr:.1f}<2 (+0)"

    # External level strength: 0–6
    ext_pts = 0; ext_d = "no ext levels"
    if lm.external_levels:
        best_e = max(lv.strength for lv in lm.external_levels)
        if best_e >= 3:   ext_pts = 6; ext_d = f"ext ★★★ (+6)"
        elif best_e >= 2: ext_pts = 4; ext_d = f"ext ★★ (+4)"
        else:             ext_pts = 2; ext_d = f"ext ★ (+2)"

    # Internal level strength: 0–4
    int_pts = 0; int_d = "no int levels"
    if lm.internal_levels:
        best_i = max(lv.strength for lv in lm.internal_levels)
        if best_i >= 3:   int_pts = 4; int_d = f"int ★★★ (+4) — tight SL zone"
        elif best_i >= 2: int_pts = 2; int_d = f"int ★★ (+2)"
        else:             int_pts = 1; int_d = f"int ★ (+1)"

    total  = min(rr_pts + ext_pts + int_pts, 20)
    detail = f"{rr_d} | {ext_d} | {int_d}"
    return total, detail


def _l4_markov(prob: float) -> tuple[int, str]:
    """Layer 4: Markov continuation probability (0–20)."""
    if prob >= 0.75: return 20, f"Mk={prob:.2f}≥0.75 — very high confidence"
    if prob >= 0.70: return 17, f"Mk={prob:.2f}≥0.70 — high confidence"
    if prob >= 0.65: return 14, f"Mk={prob:.2f}≥0.65 — solid"
    if prob >= 0.55: return 9,  f"Mk={prob:.2f}≥0.55 — moderate"
    if prob >= 0.45: return 4,  f"Mk={prob:.2f}≥0.45 — weak"
    return 0, f"Mk={prob:.2f}<0.45 — no statistical edge"


def _l5_pullback(pb_total: int, momentum: MomentumBuild) -> tuple[int, str]:
    """Layer 5: Pullback score (0–18) + momentum bonus (0–2) → capped at 20."""
    # Map 0–15 pullback total to 0–18
    if pb_total >= 13: base = 18; base_d = f"PB {pb_total}/15 MAXIMUM (+18)"
    elif pb_total >= 10: base = 14; base_d = f"PB {pb_total}/15 HIGH (+14)"
    elif pb_total >= 7:  base = 10; base_d = f"PB {pb_total}/15 MEDIUM (+10)"
    elif pb_total >= 4:  base = 5;  base_d = f"PB {pb_total}/15 WEAK (+5)"
    else:                base = 1;  base_d = f"PB {pb_total}/15 NONE (+1)"

    bonus   = momentum.entry_bonus   # +2 if building, else 0
    total   = min(base + bonus, 20)
    mom_d   = (f"momentum BUILDING +{bonus} ({momentum.detail})"
               if momentum.building
               else "no momentum build")
    detail  = f"{base_d} | {mom_d}"
    return total, detail


# ══════════════════════════════════════════════════════════════════════════════
# UNIFIED SCORER
# ══════════════════════════════════════════════════════════════════════════════

def f_score_entry(
    df:            pd.DataFrame,
    bar_idx:       int,
    direction:     int,
    bos_level:     float,
    state:         str,
    location:      str,
    instrument:    str,
    markov_prob:   float = 0.0,
    prior_pb_totals: list[int] = None,  # last N pullback totals for momentum
) -> EntryScore:
    """
    Compute the 5-layer entry score for one bar.

    prior_pb_totals : list of recent pullback totals (oldest→newest, not including
                      this bar's own total) for momentum_building detection.
                      Pass None if not available — momentum will be skipped.
    """
    row      = df.iloc[bar_idx]
    close    = float(row["close"])
    atr      = max(float(row.get("atr14", 1.0)), 1e-6)
    dt_str   = str(row.get("datetime_utc", f"bar_{bar_idx}"))

    # UTC hour
    m = re.search(r'(\d{2}):(\d{2})', dt_str)
    utc_hour = int(m.group(1)) if m else 12

    sess_valid, in_kz, sess_d, conv_pts = _session_info(instrument, utc_hour)

    # Layer 1
    l1, l1_d = _l1_structure(state)

    # Layer 2  — pass conviction_pts for calibrated instruments
    _conv_arg = conv_pts if instrument.upper() in _SESSION_CONVICTION else None
    l2, l2_d = _l2_location(location, sess_valid, in_kz, sess_d, _conv_arg)

    # Layer 3 — liquidity map
    lm = f_get_liquidity_context(
        df=df, bar_idx=bar_idx, direction=direction,
        bos_level=bos_level, instrument=instrument, state=state
    )
    l3, l3_d = _l3_liquidity(lm)

    # Layer 4 — Markov
    l4, l4_d = _l4_markov(markov_prob)

    # Layer 5 — pullback + momentum
    pb = f_score_pullback(
        df=df, bar_idx=bar_idx, direction=direction,
        bos_level=bos_level, state=state, location=location,
        instrument=instrument
    )
    # Build momentum from prior totals + current
    if prior_pb_totals is not None:
        mom = f_momentum_building(prior_pb_totals + [pb.total])
    else:
        mom = f_momentum_building([pb.total])
    pb.momentum = mom

    l5, l5_d = _l5_pullback(pb.total, mom)

    total           = l1 + l2 + l3 + l4 + l5
    grade, size_mult = _grade_and_size(total)
    rr_avail        = lm.rr_available if not np.isnan(lm.rr_available) else 0.0

    # Prop filter
    prop_reasons: list[str] = []
    if total < 65:              prop_reasons.append(f"score {total} < 65")
    if markov_prob < 0.65:      prop_reasons.append(f"Markov {markov_prob:.2f} < 0.65")
    if pb.total < 9:            prop_reasons.append(f"pullback {pb.total}/15 < 9")
    if rr_avail < 3.0:          prop_reasons.append(f"R:R {rr_avail:.1f} < 3.0")
    if state in ("DISTRIBUTION", "TREND_FAILURE"):
        prop_reasons.append(f"state={state} disqualifies")
    if not sess_valid:          prop_reasons.append(f"session invalid")

    return EntryScore(
        bar_idx           = bar_idx,
        datetime_utc      = dt_str[:16],
        instrument        = instrument,
        state             = state,
        location          = location,
        direction         = direction,
        close             = close,
        atr               = round(atr, 5),
        l1_structure      = l1,
        l2_location       = l2,
        l3_liquidity      = l3,
        l4_markov         = l4,
        l5_pullback       = l5,
        total             = total,
        grade             = grade,
        size_mult         = size_mult,
        rr_available      = round(rr_avail, 2),
        pullback_total    = pb.total,
        markov_prob       = markov_prob,
        session_valid     = sess_valid,
        in_killzone       = in_kz,
        prop_filter_pass  = len(prop_reasons) == 0,
        prop_reasons      = prop_reasons,
        l1_detail         = l1_d,
        l2_detail         = l2_d,
        l3_detail         = l3_d,
        l4_detail         = l4_d,
        l5_detail         = l5_d,
        momentum_detail   = mom.detail,
        pullback_score_obj= pb,
        liquidity_map     = lm,
        momentum          = mom,
    )


# ══════════════════════════════════════════════════════════════════════════════
# BATCH SCORER
# ══════════════════════════════════════════════════════════════════════════════

def f_score_entries_batch(
    df:            pd.DataFrame,
    state_data:    list[dict],
    instrument:    str,
    markov_map:    dict | None = None,
    states_filter: tuple[str, ...] = ("PULLBACK", "REACCUMULATION", "LIQ_SWEEP"),
) -> list[EntryScore]:
    """
    Score all candidate bars. Attaches momentum context automatically.
    Returns list sorted by total score desc.
    """
    # First pass: collect pullback scores in bar order for momentum lookback
    pb_scores = f_score_pullbacks_batch(df, state_data, instrument, states_filter)
    bar_to_pb: dict[int, PullbackScore] = {sc.bar_idx: sc for sc in pb_scores}
    sorted_bars = sorted(bar_to_pb.keys())

    results: list[EntryScore] = []
    for sr in sorted(state_data, key=lambda x: x["bar"]):
        if sr.get("state") not in states_filter:
            continue
        bar_idx   = sr["bar"]
        direction = sr.get("macro_dir", +1)
        bos_level = sr.get("bos_level") or float(df["close"].iloc[bar_idx])
        state_s   = sr.get("state", "UNKNOWN")
        location  = sr.get("location", "UNKNOWN")
        markov_p  = (markov_map or {}).get(bar_idx, 0.0)

        # Build prior_pb_totals for momentum (last 4 scored bars before this one)
        pos = sorted_bars.index(bar_idx) if bar_idx in sorted_bars else -1
        prior_totals = [
            bar_to_pb[sorted_bars[i]].total
            for i in range(max(0, pos - 4), pos)
            if sorted_bars[i] in bar_to_pb
        ]

        try:
            es = f_score_entry(
                df=df, bar_idx=bar_idx, direction=direction,
                bos_level=bos_level, state=state_s, location=location,
                instrument=instrument, markov_prob=markov_p,
                prior_pb_totals=prior_totals if prior_totals else None,
            )
            results.append(es)
        except Exception:
            pass

    results.sort(key=lambda x: x.total, reverse=True)
    return results


# ══════════════════════════════════════════════════════════════════════════════
# DIAGNOSTIC — TOP 5 XAUUSD OOS ENTRIES
# ══════════════════════════════════════════════════════════════════════════════

def _run_top5() -> None:
    from golden_highway_engine  import build_features
    from macro_structure_engine import run_macro_state_machine

    inst = "XAUUSD"
    tf   = "1H"

    print(f"\n{'═'*80}")
    print(f"  ENTRY SCORE ENGINE — {inst} {tf} — TOP 5 OOS ENTRIES")
    print(f"{'═'*80}")

    df      = build_features(inst, tf)
    n       = len(df)
    oos_s   = int(n * 0.80)
    states  = run_macro_state_machine(df, inst, tf)
    oos_st  = [s for s in states if s["bar"] >= oos_s]

    entries = f_score_entries_batch(df, oos_st, inst)
    if not entries:
        print("  No entries found"); return

    top5 = entries[:5]

    grade_sym = {"A+": "✦", "A": "●", "B": "◑", "C": "○", "SKIP": "✕"}

    for rank, es in enumerate(top5, 1):
        gs   = grade_sym.get(es.grade, "?")
        dirs = "BULL ↑" if es.direction == +1 else "BEAR ↓"
        prop = "✓ APPROVED" if es.prop_filter_pass else ("✗ BLOCKED — " + " | ".join(es.prop_reasons))
        lm   = es.liquidity_map

        print(f"\n{'─'*80}")
        print(f"  #{rank}  Bar {es.bar_idx}  {es.datetime_utc}  {dirs}  {es.instrument}")
        print(f"  State: {es.state}   Location: {es.location}")
        print(f"  Close: {es.close:,.2f}   ATR: {es.atr:.2f}   BOS: {lm.bos_level:,.2f}")
        print(f"{'─'*80}")

        rows = [
            ("L1 Macro Structure", es.l1_structure, 20, es.l1_detail),
            ("L2 Location+Session", es.l2_location,  20, es.l2_detail),
            ("L3 Liquidity",        es.l3_liquidity, 20, es.l3_detail),
            ("L4 Markov",           es.l4_markov,    20, es.l4_detail),
            ("L5 LTF Pullback",     es.l5_pullback,  20, es.l5_detail),
        ]
        for name, score, mx, detail in rows:
            filled = "█" * int(score / mx * 12)
            empty  = "░" * (12 - int(score / mx * 12))
            print(f"  {name:<20} [{score:>2}/{mx}]  {filled}{empty}  {detail}")

        print(f"  {'─'*50}")
        print(f"  TOTAL:         {es.total}/100")
        print(f"  GRADE:         {gs} {es.grade}")
        print(f"  SIZE:          {es.size_mult:.2f}×")
        print(f"  PROP:          {prop}")
        print(f"  R:R:           {es.rr_available:.1f}:1  "
              f"SL→{lm.suggested_sl:,.2f}  TP1→{lm.suggested_tp1:,.2f}  "
              f"TP2→{lm.suggested_tp2:,.2f}")
        if es.momentum.building:
            print(f"  MOMENTUM:      🔺 {es.momentum.detail}")
        else:
            print(f"  MOMENTUM:      — not building")

        # Plain English
        print(f"\n  PLAIN ENGLISH:")
        _print_narrative(es)

    # Grade distribution
    print(f"\n{'─'*80}")
    print(f"  GRADE DISTRIBUTION — all {len(entries)} candidate bars")
    print(f"{'─'*80}")
    dist: dict[str, int] = {}
    for es in entries:
        dist[es.grade] = dist.get(es.grade, 0) + 1
    for g, sym in grade_sym.items():
        cnt = dist.get(g, 0)
        pct = cnt / len(entries) * 100 if entries else 0
        bar = "█" * int(pct / 2)
        mult = {"A+": "1.00×", "A": "0.75×", "B": "0.50×",
                "C": "0.25×", "SKIP": "0×"}.get(g, "")
        print(f"  {sym} {g:<5}  {cnt:>4}  {pct:>5.1f}%  {bar}  {mult}")
    prop_n = sum(1 for e in entries if e.prop_filter_pass)
    print(f"\n  Prop filter PASS: {prop_n}/{len(entries)} ({prop_n/len(entries)*100:.1f}%)")


def _print_narrative(es: EntryScore) -> None:
    """Print a plain-English explanation of why this entry scores what it scores."""
    lm   = es.liquidity_map
    pb   = es.pullback_score_obj
    dirs = "bullish" if es.direction == +1 else "bearish"
    action = "long" if es.direction == +1 else "short"

    lines = []

    # Structure sentence
    if es.state == "PULLBACK":
        lines.append(
            f"  {es.instrument} is in a {dirs} PULLBACK at {es.location.replace('_',' ').lower()}, "
            f"meaning the prior expansion leg is intact and price is retracing to reload."
        )
    elif es.state == "LIQ_SWEEP":
        lines.append(
            f"  {es.instrument} just completed a LIQUIDITY SWEEP at {es.location.replace('_',' ').lower()}. "
            f"Stops below (or above) were hunted and price has reclaimed the level — "
            f"classic reversal setup."
        )
    elif es.state == "REACCUMULATION":
        lines.append(
            f"  {es.instrument} is in confirmed REACCUMULATION — a sideways compression "
            f"phase after an expansion, with evidence of institutional absorption."
        )
    else:
        lines.append(f"  {es.instrument} state: {es.state} / {es.location}.")

    # Pullback sentence
    if pb:
        checks_passed = sum([pb.c1_micro>0, pb.c2_location>0, pb.c3_liq_level>0,
                             pb.c4_vol_dryup>0, pb.c5_fractal>0,
                             pb.c6_htf_align>0, pb.c7_flow_turn>0])
        lines.append(
            f"  Pullback engine: {pb.total}/15 ({checks_passed}/7 checks passed). "
            f"Volume dry-up {'confirmed' if pb.c4_vol_dryup>0 else 'not confirmed'}, "
            f"fractal {'forming' if pb.c5_fractal>0 else 'absent'}, "
            f"flow {'turning' if pb.c7_flow_turn>0 else 'still declining'}."
        )

    # Momentum
    if es.momentum.building:
        lines.append(
            f"  Score ACCELERATING: {es.momentum.detail}. "
            f"This is not an isolated signal — evidence is stacking in real time (+2 bonus applied)."
        )

    # Liquidity / R:R
    if lm:
        rr = es.rr_available
        if rr >= 3.0:
            lines.append(
                f"  R:R is {rr:.1f}:1 — SL at {lm.suggested_sl:,.2f} below the "
                f"{'equal-lows cluster' if lm.internal_levels else 'BOS level'}, "
                f"TP1 at {lm.suggested_tp1:,.2f} "
                f"({'round number' if lm.external_levels and lm.external_levels[0].source=='round_number' else 'prior pivot'})."
            )
        else:
            lines.append(
                f"  R:R is only {rr:.1f}:1 — insufficient for prop-funded account. "
                f"Paper trade or reduced size only."
            )

    # HTF
    if pb and pb.c6_htf_align > 0:
        lines.append(f"  4H + 1H MTF alignment confirmed {dirs}.")
    else:
        lines.append(f"  HTF alignment mixed or opposing — reduces confidence.")

    # Verdict
    if es.grade == "A+":
        verdict = (f"  VERDICT: A+ — take the trade at full size ({es.size_mult:.2f}×). "
                   f"All layers aligned. This is the setup the system was built to find.")
    elif es.grade == "A":
        verdict = (f"  VERDICT: A — take the trade at reduced size ({es.size_mult:.2f}×). "
                   f"One layer sub-optimal but overall evidence is strong.")
    elif es.grade == "B":
        verdict = (f"  VERDICT: B — wait. Setup is building but not complete. "
                   f"Watch for one more confirmation before entering.")
    else:
        verdict = (f"  VERDICT: {es.grade} — skip or paper trade only ({es.size_mult:.2f}×).")

    if not es.prop_filter_pass:
        verdict += f" Prop filter BLOCKED: {' | '.join(es.prop_reasons)}."

    lines.append(verdict)

    for line in lines:
        # Wrap at 78 chars
        words  = line.split()
        cur    = ""
        for w in words:
            if len(cur) + len(w) + 1 > 78:
                print(cur); cur = "  " + w + " "
            else:
                cur += w + " "
        if cur.strip(): print(cur)


if __name__ == "__main__":
    _run_top5()
