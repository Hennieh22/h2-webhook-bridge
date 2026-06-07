"""
H2 Brief Generator — Phase 9
Reads live state, state stats, and signal log.
Produces outputs/H2_daily_briefing.md.

Run directly:  py -3 cowork/H2_brief_generator.py
Also called by briefing/generator.py session brief trigger.
"""

import json
import csv
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ── Paths ────────────────────────────────────────────────────────────────────

ROOT       = Path(__file__).resolve().parent.parent
OUTPUTS    = ROOT / "outputs"
LIVE_STATE = OUTPUTS / "H2_live_state.json"
SIGNAL_LOG = OUTPUTS / "H2_signal_log.csv"
BRIEF_OUT  = OUTPUTS / "H2_daily_briefing.md"
SAST       = timezone(timedelta(hours=2))

# ── Constants ─────────────────────────────────────────────────────────────────

# Sharpe weights from backtest (used for ranking tie-breaks)
SHARPE_WEIGHTS = {
    "US30":   {"1H": 15.08, "4H": 8.1,  "15m": 3.2},
    "GBPUSD": {"1H": 7.85,  "4H": 4.2,  "15m": 2.1},
    "XAUUSD": {"1H": 7.22,  "4H": 3.8,  "15m": 1.9},
    "UK100":  {"1H": 9.14,  "4H": 5.1,  "15m": 2.4},
    "DE40":   {"1H": 7.13,  "4H": 3.9,  "15m": 1.8},
    "USTEC":  {"1H": 7.65,  "4H": 4.0,  "15m": 2.0},
    "EURJPY": {"1H": 4.28,  "4H": 2.3,  "15m": 1.1},
    "EURUSD": {"1H": 4.22,  "4H": 2.2,  "15m": 1.0},
    "GBPJPY": {"1H": 3.11,  "4H": 1.7,  "15m": 0.8},
    "USDJPY": {"1H": 3.83,  "4H": 2.0,  "15m": 1.0},
    "AUDUSD": {"1H": 1.46,  "4H": 0.8,  "15m": 0.4},
    "USDCAD": {"1H": 1.78,  "4H": 1.0,  "15m": 0.5},
    "JP225":  {"1H": 2.00,  "4H": 1.0,  "15m": 0.5},
    "HK50":   {"1H": 3.12,  "4H": 1.6,  "15m": 0.8},
    "XAGUSD": {"1H": 2.50,  "4H": 1.3,  "15m": 0.6},
}

# Instrument priority order (from BRIEF_PROMPT)
PRIORITY_ORDER = [
    "US30", "GBPUSD", "DE40", "XAUUSD", "GBPJPY",
    "EURJPY", "EURUSD", "USDJPY", "USTEC", "UK100",
    "AUDUSD", "USDCAD", "XAGUSD",
]

# Always avoid / manual only
AVOID_INSTRUMENTS = {"JP225", "HK50", "AUS200"}
MANUAL_WATCH      = {"JP225"}

# Session windows (UTC hours)
SESSIONS = {
    "Tokyo":   (0,  9),
    "London":  (7,  16),
    "NY":      (13, 22),
    "Overlap": (13, 16),
}

KILL_ZONES_UTC = [
    {"name": "London Open",  "hour": 7,  "minute": 0},
    {"name": "NY Open",      "hour": 13, "minute": 0},
    {"name": "London Close", "hour": 16, "minute": 0},
]

# ── Helpers ───────────────────────────────────────────────────────────────────

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def utc_to_sast(dt: datetime) -> datetime:
    return dt.astimezone(SAST)


def current_session(utc_hour: int) -> str:
    if 13 <= utc_hour < 16:
        return "London/NY Overlap"
    if 13 <= utc_hour < 22:
        return "NY"
    if 7 <= utc_hour < 16:
        return "London"
    if 0 <= utc_hour < 9:
        return "Tokyo"
    return "Off-Hours"


def next_kill_zone(now: datetime) -> dict:
    """Return name and minutes until next kill zone."""
    best = None
    for kz in KILL_ZONES_UTC:
        candidate = now.replace(hour=kz["hour"], minute=kz["minute"],
                                second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        diff = int((candidate - now).total_seconds() / 60)
        if best is None or diff < best["minutes"]:
            best = {"name": kz["name"], "minutes": diff}
    return best


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_state_stats(instrument: str, timeframe: str) -> dict:
    path = OUTPUTS / f"H2_state_stats_{instrument}_{timeframe}.json"
    return load_json(path)


def load_signal_log_today(today_date: str) -> list[dict]:
    """Return today's rows from H2_signal_log.csv."""
    rows = []
    if not SIGNAL_LOG.exists():
        return rows
    with open(SIGNAL_LOG, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("timestamp_utc", "").startswith(today_date):
                rows.append(row)
    return rows


def tier_label(tier: str) -> str:
    return {"GREEN": "🟢", "AMBER": "🟡", "RED": "🔴", "AVOID": "⛔"}.get(tier, "⚪")


def conviction_score_from_data(inst_data: dict) -> float:
    """Recompute conviction score from live state fields."""
    prob  = 0.0
    if inst_data.get("next_states"):
        prob = inst_data["next_states"][0].get("probability", 0.0)
    ev        = max(0.0, inst_data.get("historical_ev", 0.0))
    stability = inst_data.get("stability_score", 0.0)
    pillars   = inst_data.get("pillars_confirmed", 0) / 4.0
    score = (
        prob      * 0.35 +
        (ev / 3.0) * 0.25 +   # normalise EV (cap at 3R = full weight)
        stability * 0.20 +
        pillars   * 0.20
    )
    return min(score, 1.0)


def pillar_signals(state_id: str, inst_data: dict) -> dict:
    """
    Generate plain-English pillar confirmation signals
    from the state ID and live data.
    """
    parts     = state_id.split("_") if state_id else []
    trend     = parts[0] if len(parts) > 0 else "NEUTRAL"
    momentum  = parts[1] if len(parts) > 1 else "NEUTRAL"
    vol       = parts[2] if len(parts) > 2 else "NORMAL"
    liquidity = parts[3] if len(parts) > 3 else "INSIDE"
    session   = parts[4] if len(parts) > 4 else "OFFHOURS"
    structure = parts[5] if len(parts) > 5 else "RANGE"
    rsi_zone  = parts[6] if len(parts) > 6 else "NEUTRAL"

    direction = "long" if trend == "BULL" else "short" if trend == "BEAR" else "flat"

    # Volume pillar
    if momentum == "STRONG":
        vol_signal = f"RVOL ≥1.5×, delta positive — expansion confirming"
    elif structure in ("BOS", "MSS"):
        vol_signal = f"Watch for RVOL spike ≥1.5× on breakout bar"
    elif structure == "CHOCH":
        vol_signal = f"Delta flip required — sell→buy for {direction} CHoCH"
    elif structure == "PULLBACK":
        vol_signal = f"Volume dry-up on retrace, RVOL < 0.8× average"
    else:
        vol_signal = f"RVOL neutral — wait for expansion before entry"

    # Structure pillar
    if structure == "BOS":
        struct_signal = f"BOS printed — enter on retest of broken level"
    elif structure == "CHOCH":
        struct_signal = f"CHoCH confirmed — wait for LTF BOS in {direction} direction"
    elif structure == "MSS":
        struct_signal = f"MSS printed — strongest structure signal, enter at retest"
    elif structure == "PULLBACK":
        struct_signal = f"HL holding, no CHoCH — structure intact for {direction}"
    elif structure == "LIQ_SWEEP":
        struct_signal = f"Liquidity sweep — wait for reclaim within 3 bars"
    else:
        struct_signal = f"Range / continuation — no BOS yet, wait for break"

    # Fractals pillar
    if structure == "BOS":
        frac_signal = f"Fractal HL must form and hold after pullback"
    elif structure == "PULLBACK":
        frac_signal = f"LTF fractal high at 50–62% retrace zone — key entry trigger"
    elif structure == "LIQ_SWEEP":
        frac_signal = f"Fractal swept — price must reclaim within 1–3 bars"
    elif rsi_zone == "OB":
        frac_signal = f"Equal highs forming — external liquidity target above"
    elif rsi_zone == "OS":
        frac_signal = f"Equal lows forming — external liquidity target below"
    else:
        frac_signal = f"Watch for fractal HL/LH formation at structure level"

    # MTF pillar
    next_states = inst_data.get("next_states", [])
    top_next    = next_states[0].get("state", "") if next_states else ""
    top_trend   = top_next.split("_")[0] if top_next else trend

    if trend == "BULL" and structure in ("BOS", "MSS"):
        mtf_signal = f"4H BULL + 1H {structure} — require 5m BOS up before entry"
    elif trend == "BULL" and structure == "PULLBACK":
        mtf_signal = f"4H BULL + 1H PULLBACK — enter on 5m BOS up from HL"
    elif trend == "BEAR" and structure in ("BOS", "MSS"):
        mtf_signal = f"4H BEAR + 1H {structure} — require 5m BOS down before entry"
    elif trend == "BEAR" and structure == "PULLBACK":
        mtf_signal = f"4H BEAR + 1H PULLBACK — enter on 5m BOS down from LH"
    elif structure == "CHOCH":
        mtf_signal = f"4H {trend} + 1H CHoCH — wait for MSS on 5m to confirm reversal"
    else:
        mtf_signal = f"4H and 1H must agree on {direction} before entry"

    # Invalidation
    if structure in ("BOS", "MSS", "PULLBACK") and trend == "BULL":
        invalid = f"Price closes below the BOS level or HL breaks — exit plan invalid"
    elif structure in ("BOS", "MSS", "PULLBACK") and trend == "BEAR":
        invalid = f"Price closes above the BOS level or LH breaks — exit plan invalid"
    else:
        invalid = f"CHoCH prints on entry TF before position is established"

    return {
        "volume":    vol_signal,
        "structure": struct_signal,
        "fractals":  frac_signal,
        "mtf":       mtf_signal,
        "invalid":   invalid,
    }


def situation_text(inst: str, state_id: str, inst_data: dict) -> str:
    """Generate 2–3 sentence plain-English situation description."""
    parts     = state_id.split("_") if state_id else []
    trend     = parts[0] if len(parts) > 0 else "NEUTRAL"
    momentum  = parts[1] if len(parts) > 1 else "NEUTRAL"
    vol       = parts[2] if len(parts) > 2 else "NORMAL"
    structure = parts[5] if len(parts) > 5 else "RANGE"
    rsi_zone  = parts[6] if len(parts) > 6 else "NEUTRAL"
    session   = inst_data.get("session", "OFFHOURS")

    next_states = inst_data.get("next_states", [])
    top_next    = next_states[0] if next_states else {}
    top_state   = top_next.get("state", "")
    top_prob    = top_next.get("probability", 0.0)
    top_ev      = top_next.get("ev", 0.0)

    # Sentence 1 — current situation
    direction = "bullish" if trend == "BULL" else "bearish" if trend == "BEAR" else "neutral"
    mom_str   = "strong" if momentum == "STRONG" else "weak" if momentum == "WEAK" else "neutral"
    vol_str   = "high-volatility" if vol == "HIGH" else "low-volatility" if vol == "LOW" else "normal"
    struct_str = {
        "BOS":       "break of structure confirmed",
        "CHOCH":     "change of character printed",
        "MSS":       "market structure shift — strongest reversal signal",
        "PULLBACK":  "in a structured pullback",
        "LIQ_SWEEP": "liquidity sweep in progress",
        "TREND_CONT":"in trend continuation",
        "RANGE":     "range-bound",
    }.get(structure, structure)

    s1 = f"{inst} is {direction} with {mom_str} momentum — {struct_str} in {vol_str} conditions during the {session} session."

    # Sentence 2 — next state forecast
    if top_state and top_prob > 0:
        top_parts  = top_state.split("_")
        top_trend  = top_parts[0] if top_parts else "?"
        top_struct = top_parts[5] if len(top_parts) > 5 else "?"
        ev_str     = f"+{top_ev:.1f}R" if top_ev >= 0 else f"{top_ev:.1f}R"
        s2 = (f"Markov matrix forecasts {top_trend} {top_struct} as the most likely next state "
              f"({top_prob*100:.0f}% probability, {ev_str} EV).")
    else:
        s2 = "Transition matrix has insufficient data — wait for next bar update."

    # Sentence 3 — action cue
    if rsi_zone == "OB" and trend == "BULL":
        s3 = "RSI overbought — wait for pullback to BOS level before entering long."
    elif rsi_zone == "OS" and trend == "BEAR":
        s3 = "RSI oversold — wait for dead-cat bounce to exhaust before entering short."
    elif structure == "PULLBACK":
        s3 = "Do not enter until fractal HL prints and holds on the entry timeframe."
    elif structure in ("BOS", "MSS"):
        s3 = "Enter on first retest of the broken level with volume confirmation."
    else:
        s3 = "Wait for structure event before committing — no entry in compression."

    return f"{s1} {s2} {s3}"


def regime_summary(instruments: dict) -> dict:
    """Classify overall session regime from live state."""
    bull_count  = sum(1 for v in instruments.values()
                      if v.get("current_state", "").startswith("BULL"))
    bear_count  = sum(1 for v in instruments.values()
                      if v.get("current_state", "").startswith("BEAR"))
    green_count = sum(1 for v in instruments.values() if v.get("tier") == "GREEN")
    amber_count = sum(1 for v in instruments.values() if v.get("tier") == "AMBER")
    avoid_count = sum(1 for v in instruments.values() if v.get("tier") in ("AVOID", "RED"))

    comp_states = [k for k, v in instruments.items()
                   if "RANGE" in v.get("current_state", "") or
                      "COMPRESSION" in v.get("current_state", "")]

    total = len(instruments)
    if bull_count / max(total, 1) >= 0.55:
        bias = "Bullish"
    elif bear_count / max(total, 1) >= 0.55:
        bias = "Bearish"
    else:
        bias = "Mixed"

    if green_count >= 3:
        character = "Trending"
    elif avoid_count >= total * 0.5:
        character = "Compressing"
    else:
        character = "Ranging"

    return {
        "bias":        bias,
        "character":   character,
        "green":       green_count,
        "amber":       amber_count,
        "avoid":       avoid_count,
        "compression": comp_states,
    }


# ── Main briefing builder ──────────────────────────────────────────────────────

def build_briefing() -> str:
    now      = now_utc()
    sast_now = utc_to_sast(now)
    today    = now.strftime("%Y-%m-%d")

    sast_str = sast_now.strftime("%d %b %Y · %H:%M SAST")
    session  = current_session(now.hour)
    kz       = next_kill_zone(now)
    kz_str   = f"{kz['name']} in {kz['minutes']} min"

    # ── Load data ────────────────────────────────────────────────────────────
    live = load_json(LIVE_STATE)
    instruments: dict = live.get("instruments", {})
    signal_rows = load_signal_log_today(today)

    if not instruments:
        return f"# H2 SESSION BRIEF\n\n⚠ No live state data found. Run live/monitor.py first.\n"

    # ── Score and classify all instruments ───────────────────────────────────
    ranked = []
    avoid_list  = []
    manual_watch = []

    for inst, data in instruments.items():
        tf        = data.get("timeframe", "1H")
        state_id  = data.get("current_state", "UNKNOWN")
        tier      = data.get("tier", "RED")
        conv      = data.get("conviction", "SKIP")
        pillars   = data.get("pillars_confirmed", 0)
        all_gates = data.get("all_gates_pass", False)
        samples   = data.get("sample_count", 0)
        ruin      = data.get("ruin_pct", 0.0)
        score     = data.get("conviction_score", conviction_score_from_data(data))
        sharpe    = SHARPE_WEIGHTS.get(inst, {}).get(tf, 1.0)
        priority  = PRIORITY_ORDER.index(inst) if inst in PRIORITY_ORDER else 99

        next_states = data.get("next_states", [])
        top_next    = next_states[0] if next_states else {}

        row = {
            "inst":       inst,
            "tf":         tf,
            "state_id":   state_id,
            "tier":       tier,
            "conv":       conv,
            "pillars":    pillars,
            "all_gates":  all_gates,
            "samples":    samples,
            "ruin":       ruin,
            "score":      score,
            "sharpe":     sharpe,
            "priority":   priority,
            "wr":         data.get("historical_wr", 0.0),
            "ev":         data.get("historical_ev", 0.0),
            "gates_n":    sum(1 for g in data.get("gates", {}).values()
                              if isinstance(g, dict) and g.get("pass")),
            "top_next":   top_next,
            "data":       data,
        }

        if inst in MANUAL_WATCH:
            manual_watch.append(row)
        elif inst in AVOID_INSTRUMENTS or tier == "AVOID" or ruin > 5.0:
            avoid_list.append(row)
        else:
            ranked.append(row)

    # Sort: tier order then score × sharpe
    tier_order = {"GREEN": 0, "AMBER": 1, "RED": 2, "AVOID": 3}
    ranked.sort(key=lambda r: (
        tier_order.get(r["tier"], 4),
        -r["score"] * r["sharpe"],
        r["priority"],
    ))

    green_setups = [r for r in ranked if r["tier"] == "GREEN"]
    amber_setups = [r for r in ranked if r["tier"] == "AMBER"]
    top5         = (green_setups + amber_setups)[:5]
    if not top5:
        top5 = ranked[:5]

    regime = regime_summary(instruments)

    # ── Signal log analysis ───────────────────────────────────────────────────
    fired_today  = [r for r in signal_rows if r.get("fired") == "True"]
    failed_today = [r for r in signal_rows
                    if r.get("outcome_r") and float(r["outcome_r"] or 0) < 0]

    # ── Avoid list assembly ───────────────────────────────────────────────────
    avoid_reasons = {}
    for r in avoid_list:
        reasons = []
        if r["inst"] in AVOID_INSTRUMENTS:
            if r["inst"] == "HK50":
                reasons.append("ruin 6.7% — system excluded")
            else:
                reasons.append("system excluded — manual watch only")
        if r["ruin"] > 5.0 and r["inst"] not in AVOID_INSTRUMENTS:
            reasons.append(f"ruin {r['ruin']:.1f}% > 5% threshold")
        if r["samples"] < 30:
            reasons.append(f"only {r['samples']} samples — below 30 minimum")
        if r["tier"] == "AVOID":
            reasons.append("conviction score below threshold")
        avoid_reasons[r["inst"]] = ", ".join(reasons) if reasons else "system rule"

    compression_list = regime["compression"]
    for c in compression_list:
        if c not in avoid_reasons:
            avoid_reasons[c] = "compression / range — no directional edge"

    # ── Build markdown ────────────────────────────────────────────────────────
    lines = []

    # Header
    lines += [
        "# H2 SESSION BRIEF",
        f"**{sast_str} | Session: {session}**",
        f"**Kill zone: {kz_str}**",
        "",
        "---",
        "",
    ]

    # Market regime
    lines += [
        "## MARKET REGIME",
        "| Metric | Value |",
        "|---|---|",
        f"| Session character | {regime['character']} |",
        f"| Overall bias | {regime['bias']} |",
        f"| GREEN setups | {regime['green']} |",
        f"| AMBER setups | {regime['amber']} |",
        f"| AVOID count | {regime['avoid']} |",
        "",
        "---",
        "",
    ]

    # Top 5 setups
    if green_setups:
        lines.append("## TOP 5 RIGHT NOW 🟢")
    else:
        lines += [
            "## TOP 5 RIGHT NOW 🟡",
            "> **No A+ setups active — patience required.**",
            "> Check again at next session open.",
            "",
        ]
    lines.append("")

    for rank, row in enumerate(top5, 1):
        inst      = row["inst"]
        tf        = row["tf"]
        state_id  = row["state_id"]
        data      = row["data"]
        top_next  = row["top_next"]
        pillars   = data.get("confirmation_pillars", {})
        sigs      = pillar_signals(state_id, data)
        situation = situation_text(inst, state_id, data)

        next_state_str = (
            f"`{top_next.get('state','—')}` at "
            f"{top_next.get('probability', 0)*100:.0f}%"
            if top_next else "— insufficient data"
        )
        ev_str  = f"{row['ev']:+.2f}R"
        wr_str  = f"{row['wr']*100:.1f}%" if row["wr"] > 0 else "—"
        gates_n = row["gates_n"]

        lines += [
            f"### {rank}. {inst} · {tf} · {row['conv']} · {data.get('session','—')}",
            f"**State:** `{state_id}`",
            f"**Situation:** {situation}",
            "",
            "| Metric | Value |",
            "|---|---|",
            f"| Top next state | {next_state_str} |",
            f"| Expected move | {ev_str} |",
            f"| Historical WR | {wr_str} over {row['samples']} trades |",
            f"| Gates passing | {gates_n}/5 |",
            f"| Pillars confirmed | {row['pillars']}/4 |",
            "",
            "**Look for before entering:**",
            f"- Volume: {sigs['volume']}",
            f"- Structure: {sigs['structure']}",
            f"- Fractals: {sigs['fractals']}",
            f"- MTF: {sigs['mtf']}",
            "",
            f"**Do NOT enter if:** {sigs['invalid']}",
            "",
            "---",
            "",
        ]

    # Full instrument table
    lines += [
        "## FULL INSTRUMENT TABLE",
        "",
        "| # | 🚦 | Instrument | TF | State | Next State | P% | EV | WR | Pillars | Score |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]

    for i, row in enumerate(ranked, 1):
        top_next = row["top_next"]
        next_str = top_next.get("state", "—")[:30] if top_next else "—"
        prob_str = f"{top_next.get('probability',0)*100:.0f}%" if top_next else "—"
        ev_str   = f"{row['ev']:+.2f}R"
        wr_str   = f"{row['wr']*100:.0f}%" if row["wr"] > 0 else "—"
        score_str = f"{row['score']:.2f}"
        lines.append(
            f"| {i} | {tier_label(row['tier'])} | {row['inst']} | {row['tf']} "
            f"| {row['state_id'][:28]} | {next_str} | {prob_str} "
            f"| {ev_str} | {wr_str} | {row['pillars']}/4 | {score_str} |"
        )

    lines += ["", "---", ""]

    # JP225 manual watch section
    if manual_watch:
        lines += ["## MANUAL WATCH 👁", ""]
        for row in manual_watch:
            data     = row["data"]
            state_id = row["state_id"]
            lines += [
                f"**{row['inst']} · {row['tf']}** — `{state_id}`",
                f"{data.get('state_description', '')}",
                "",
                "> Revisit after data expansion — manual discretion only.",
                "",
            ]
        lines += ["---", ""]

    # Avoid list
    lines += ["## AVOID LIST ⛔", "", "| Instrument | Reason |", "|---|---|"]
    for inst, reason in sorted(avoid_reasons.items()):
        lines.append(f"| {inst} | {reason} |")
    lines += ["", "---", ""]

    # Risk flags
    comp_str = ", ".join(compression_list) if compression_list else "None"
    lines += [
        "## RISK FLAGS ⚠️",
        f"News next 2 hours: None (check ForexFactory manually before entry)",
        f"Signals fired today: {len(fired_today)}",
        f"Failed today: {len(failed_today)}",
        f"In compression: {comp_str}",
        "",
        "---",
        "",
    ]

    # Signals fired today
    lines += [
        "## SIGNALS FIRED TODAY",
        "",
        "| SAST | Instrument | Dir | Conv | EV | Outcome |",
        "|---|---|---|---|---|---|",
    ]
    if fired_today:
        for row in fired_today:
            sast_t   = row.get("sast_time", "")[:16]
            outcome  = row.get("outcome_r", "—") or "—"
            ev_val   = row.get("ev", "—")
            lines.append(
                f"| {sast_t} | {row.get('instrument','')} | "
                f"{row.get('direction','')} | {row.get('conviction','')} | "
                f"{ev_val} | {outcome} |"
            )
    else:
        lines.append("| — | No signals fired today | — | — | — | — |")

    lines += ["", "---", ""]

    # YOUR NEXT ACTION
    best = top5[0] if top5 else None
    if best:
        inst     = best["inst"]
        tf       = best["tf"]
        state_id = best["state_id"]
        sigs     = pillar_signals(state_id, best["data"])
        top_next = best["top_next"]
        next_str = top_next.get("state", "") if top_next else ""
        next_parts  = next_str.split("_")
        next_struct = next_parts[5] if len(next_parts) > 5 else "BOS"
        direction   = "long" if state_id.startswith("BULL") else "short"

        action = (
            f"Focus on **{inst} {tf}**. "
            f"The Markov matrix forecasts {next_struct} as the next state — "
            f"wait for {sigs['structure'].lower()} and {sigs['volume'].lower()} "
            f"before entering {direction}. "
            f"Do not enter until {sigs['invalid'].lower()}."
        )
    else:
        action = (
            "No qualified setups active at this time. "
            "Monitor the next session open for state transitions. "
            "Patience is a position."
        )

    lines += [
        "## YOUR NEXT ACTION",
        "",
        action,
        "",
        "---",
        f"*H2 Quant System v1 · Generated {sast_now.strftime('%Y-%m-%d %H:%M SAST')}*",
    ]

    return "\n".join(lines)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    # Force UTF-8 on Windows consoles that default to cp1252
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    print("=" * 60)
    print("H2 BRIEF GENERATOR")
    print(f"Running at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 60)

    briefing = build_briefing()

    # Save
    BRIEF_OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(BRIEF_OUT, "w", encoding="utf-8") as f:
        f.write(briefing)

    print(briefing)
    print()
    print("=" * 60)
    print(f"Saved -> {BRIEF_OUT}")
    print("=" * 60)


if __name__ == "__main__":
    main()
