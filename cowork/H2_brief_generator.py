"""
H2 Brief Generator — Phase 9 v2.0
Produces outputs/H2_daily_briefing.md in the full 7-section format.

Run directly:  python cowork/H2_brief_generator.py
Also callable: from cowork.H2_brief_generator import build_briefing
"""

import csv
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────

ROOT       = Path(__file__).resolve().parent.parent
OUTPUTS    = ROOT / "outputs"
LIVE_STATE = OUTPUTS / "H2_live_state.json"
SIGNAL_LOG = OUTPUTS / "H2_signal_log.csv"
BRIEF_OUT  = OUTPUTS / "H2_daily_briefing.md"
SAST_TZ    = timezone(timedelta(hours=2))

# ── Static data ───────────────────────────────────────────────────────────────

SHARPE_WEIGHTS = {
    "US30":   {"1H": 15.08, "4H": 8.1,  "15m": 3.2},
    "GBPUSD": {"1H":  7.85, "4H": 4.2,  "15m": 2.1},
    "XAUUSD": {"1H":  7.22, "4H": 3.8,  "15m": 1.9},
    "UK100":  {"1H":  9.14, "4H": 5.1,  "15m": 2.4},
    "DE40":   {"1H":  7.13, "4H": 3.9,  "15m": 1.8},
    "USTEC":  {"1H":  7.65, "4H": 4.0,  "15m": 2.0},
    "EURJPY": {"1H":  4.28, "4H": 2.3,  "15m": 1.1},
    "EURUSD": {"1H":  4.22, "4H": 2.2,  "15m": 1.0},
    "GBPJPY": {"1H":  3.11, "4H": 1.7,  "15m": 0.8},
    "USDJPY": {"1H":  3.83, "4H": 2.0,  "15m": 1.0},
    "AUDUSD": {"1H":  1.46, "4H": 0.8,  "15m": 0.4},
    "USDCAD": {"1H":  1.78, "4H": 1.0,  "15m": 0.5},
    "JP225":  {"1H":  2.00, "4H": 1.0,  "15m": 0.5},
    "HK50":   {"1H":  3.12, "4H": 1.6,  "15m": 0.8},
    "XAGUSD": {"1H":  2.50, "4H": 1.3,  "15m": 0.6},
}

PRIORITY_ORDER = [
    "US30", "GBPUSD", "DE40", "XAUUSD", "GBPJPY",
    "EURJPY", "EURUSD", "USDJPY", "USTEC", "UK100",
    "AUDUSD", "USDCAD", "XAGUSD",
]

# Always excluded from signals — shown only in their dedicated sections
AVOID_ALWAYS  = {"HK50", "AUS200"}
MANUAL_WATCH  = {"JP225"}
ALL_EXCLUDED  = AVOID_ALWAYS | MANUAL_WATCH

AVOID_REASONS_PERMANENT = {
    "AUS200": "order rejection unresolved at broker",
    "HK50":   "ruin 6.7%, drawdown -5.9R — system excluded",
    "JP225":  "systematic signals excluded (manual watch only — ruin 14%)",
}

KILL_ZONES_UTC = [
    {"name": "London Open",  "hour": 7,  "minute": 0},
    {"name": "NY Open",      "hour": 13, "minute": 0},
    {"name": "London Close", "hour": 16, "minute": 0},
]

TIER_ICON = {"GREEN": "🟢", "AMBER": "🟡", "RED": "🔴", "AVOID": "⛔"}
NEXT_SESSION_SAST = {
    "Tokyo":   "09:00",   # London Open SAST
    "London":  "15:00",   # NY Open SAST
    "Overlap": "15:00",
    "NY":      "09:00 (tomorrow)",
    "Off-Hours": "09:00",
}

# ── Time helpers ──────────────────────────────────────────────────────────────

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def to_sast(dt: datetime) -> datetime:
    return dt.astimezone(SAST_TZ)

def current_session(h: int) -> str:
    if 13 <= h < 16: return "London/NY Overlap"
    if 16 <= h < 22: return "NY"
    if  7 <= h < 16: return "London"
    if  0 <= h <  9: return "Tokyo"
    return "Off-Hours"

def kill_zone_status(now: datetime) -> str:
    """Return 'OPEN NOW' if within 15 min of a kill zone, else 'X in Y min'."""
    now_mins = now.hour * 60 + now.minute
    best_name  = None
    best_delta = None

    for kz in KILL_ZONES_UTC:
        kz_mins = kz["hour"] * 60 + kz["minute"]
        # distance forward
        delta_fwd = (kz_mins - now_mins) % (24 * 60)

        if delta_fwd <= 15:
            return f"{kz['name']} OPEN NOW"

        if best_delta is None or delta_fwd < best_delta:
            best_delta = delta_fwd
            best_name  = kz["name"]

    hrs, mins = divmod(best_delta, 60)
    if hrs:
        return f"{best_name} in {hrs}h {mins}m"
    return f"{best_name} in {mins} min"

# ── Data loaders ──────────────────────────────────────────────────────────────

def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)

def load_signal_log_today(today: str) -> list:
    rows = []
    if not SIGNAL_LOG.exists():
        return rows
    with open(SIGNAL_LOG, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("timestamp_utc", "").startswith(today):
                rows.append(row)
    return rows

# ── Scoring ───────────────────────────────────────────────────────────────────

def conviction_score(data: dict) -> float:
    prob      = data.get("next_states", [{}])[0].get("probability", 0.0) if data.get("next_states") else 0.0
    ev        = max(0.0, data.get("historical_ev", 0.0))
    stability = data.get("stability_score", 0.0)
    pillars   = data.get("pillars_confirmed", 0) / 4.0
    score = prob * 0.35 + (ev / 3.0) * 0.25 + stability * 0.20 + pillars * 0.20
    return round(min(score, 1.0), 4)

def assign_tier(data: dict, score: float) -> str:
    gates_pass = data.get("all_gates_pass", False)
    gates_n    = sum(1 for g in data.get("gates", {}).values()
                     if isinstance(g, dict) and g.get("pass"))
    samples    = data.get("sample_count", 0)
    ruin       = data.get("ruin_pct", 0.0)

    if samples < 30:
        return "AVOID"
    if ruin > 5.0:
        return "RED"
    if score > 0.65 and gates_pass and samples >= 30:
        return "GREEN"
    if 0.40 <= score <= 0.65 or gates_n >= 4:
        return "AMBER"
    return "RED"

def gates_count(data: dict) -> int:
    return sum(1 for g in data.get("gates", {}).values()
               if isinstance(g, dict) and g.get("pass"))

# ── Text generation ───────────────────────────────────────────────────────────

def pillar_signals(state_id: str, data: dict) -> dict:
    parts     = (state_id or "").split("_")
    trend     = parts[0] if len(parts) > 0 else "NEUTRAL"
    momentum  = parts[1] if len(parts) > 1 else "NEUTRAL"
    structure = parts[5] if len(parts) > 5 else "RANGE"
    rsi_zone  = parts[6] if len(parts) > 6 else "NEUTRAL"
    direction = "long" if trend == "BULL" else "short" if trend == "BEAR" else "flat"

    # Volume
    if momentum == "STRONG":
        vol = "RVOL >=1.5x, delta positive — expansion confirming"
    elif structure in ("BOS", "MSS"):
        vol = "Watch for RVOL spike >=1.5x on breakout bar"
    elif structure == "CHOCH":
        vol = f"Delta flip required — sell to buy for {direction} CHoCH"
    elif structure == "PULLBACK":
        vol = "Volume dry-up on retrace, RVOL < 0.8x average"
    else:
        vol = "RVOL neutral — wait for expansion before entry"

    # Structure
    if structure == "BOS":
        struct = "BOS printed — enter on first retest of broken level"
    elif structure == "CHOCH":
        struct = f"CHoCH confirmed — wait for LTF BOS in {direction} direction"
    elif structure == "MSS":
        struct = "MSS printed — strongest signal, enter at retest"
    elif structure == "PULLBACK":
        struct = f"HL holding, no CHoCH — structure intact for {direction}"
    elif structure == "LIQ_SWEEP":
        struct = "Liquidity sweep — wait for reclaim within 3 bars"
    else:
        struct = "Range / continuation — no BOS yet, wait for break"

    # Fractals
    if structure == "BOS":
        frac = "Fractal HL must form and hold after pullback"
    elif structure == "PULLBACK":
        frac = "LTF fractal high at 50-62% retrace zone — key entry trigger"
    elif structure == "LIQ_SWEEP":
        frac = "Fractal swept — price must reclaim within 1-3 bars"
    elif rsi_zone == "OB":
        frac = "Equal highs forming — external liquidity target above"
    elif rsi_zone == "OS":
        frac = "Equal lows forming — external liquidity target below"
    else:
        frac = "Watch for fractal HL/LH formation at structure level"

    # MTF
    next_states = data.get("next_states", [])
    if trend == "BULL" and structure in ("BOS", "MSS"):
        mtf = f"4H BULL + 1H {structure} — require 5m BOS up before entry"
    elif trend == "BULL" and structure == "PULLBACK":
        mtf = "4H BULL + 1H PULLBACK — enter on 5m BOS up from HL"
    elif trend == "BEAR" and structure in ("BOS", "MSS"):
        mtf = f"4H BEAR + 1H {structure} — require 5m BOS down before entry"
    elif trend == "BEAR" and structure == "PULLBACK":
        mtf = "4H BEAR + 1H PULLBACK — enter on 5m BOS down from LH"
    elif structure == "CHOCH":
        mtf = f"4H {trend} + 1H CHoCH — wait for MSS on 5m to confirm reversal"
    else:
        mtf = f"4H and 1H must agree on {direction} before entry"

    # Invalidation
    if structure in ("BOS", "MSS", "PULLBACK") and trend == "BULL":
        invalid = "Price closes below the BOS level or HL breaks"
    elif structure in ("BOS", "MSS", "PULLBACK") and trend == "BEAR":
        invalid = "Price closes above the BOS level or LH breaks"
    else:
        invalid = "CHoCH prints on entry TF before position is established"

    return {"volume": vol, "structure": struct, "fractals": frac,
            "mtf": mtf, "invalid": invalid}


def situation_text(inst: str, state_id: str, data: dict) -> str:
    parts     = (state_id or "").split("_")
    trend     = parts[0] if len(parts) > 0 else "NEUTRAL"
    momentum  = parts[1] if len(parts) > 1 else "NEUTRAL"
    vol       = parts[2] if len(parts) > 2 else "NORMAL"
    structure = parts[5] if len(parts) > 5 else "RANGE"
    rsi_zone  = parts[6] if len(parts) > 6 else "NEUTRAL"
    session   = data.get("session", "OFFHOURS")

    next_states = data.get("next_states", [])
    top_next    = next_states[0] if next_states else {}
    top_state   = top_next.get("state", "")
    top_prob    = top_next.get("probability", 0.0)
    top_ev      = top_next.get("ev", 0.0)

    # Sentence 1
    dir_str  = "bullish" if trend == "BULL" else "bearish" if trend == "BEAR" else "neutral"
    mom_str  = "strong" if momentum == "STRONG" else "weak" if momentum == "WEAK" else "neutral"
    vol_str  = "high-volatility" if vol == "HIGH" else "low-volatility" if vol == "LOW" else "normal"
    struct_map = {
        "BOS": "break of structure confirmed",
        "CHOCH": "change of character printed",
        "MSS": "market structure shift",
        "PULLBACK": "structured pullback in progress",
        "LIQ_SWEEP": "liquidity sweep in progress",
        "TREND_CONT": "trend continuation",
        "RANGE": "range-bound",
    }
    struct_str = struct_map.get(structure, structure.lower())
    s1 = (f"{inst} is {dir_str} with {mom_str} momentum — "
          f"{struct_str} in {vol_str} conditions ({session} session).")

    # Sentence 2 — Markov forecast
    if top_state and top_prob > 0:
        tp = top_state.split("_")
        t_trend  = tp[0] if tp else "?"
        t_struct = tp[5] if len(tp) > 5 else "?"
        ev_sign  = f"+{top_ev:.1f}R" if top_ev >= 0 else f"{top_ev:.1f}R"
        s2 = (f"Markov matrix forecasts {t_trend} {t_struct} as the most likely "
              f"next state ({top_prob*100:.0f}% probability, {ev_sign} EV).")
    else:
        s2 = "Transition matrix has insufficient data — wait for next cycle."

    # Sentence 3 — entry cue
    if rsi_zone == "OB" and trend == "BULL":
        s3 = "RSI overbought — wait for pullback to BOS level before entering long."
    elif rsi_zone == "OS" and trend == "BEAR":
        s3 = "RSI oversold — wait for dead-cat bounce to exhaust before shorting."
    elif structure == "PULLBACK":
        s3 = "Do not enter until fractal HL prints and holds on the entry timeframe."
    elif structure in ("BOS", "MSS"):
        s3 = "Enter on first retest of the broken level with volume confirmation."
    elif structure == "CHOCH":
        s3 = "Wait for LTF BOS in the new direction before committing."
    else:
        s3 = "Wait for a structure event — no entry in compression or range."

    return f"{s1} {s2} {s3}"


def market_character(instruments: dict) -> str:
    total      = max(len(instruments), 1)
    bull_pct   = sum(1 for v in instruments.values()
                     if v.get("current_state","").startswith("BULL")) / total
    bear_pct   = sum(1 for v in instruments.values()
                     if v.get("current_state","").startswith("BEAR")) / total
    range_pct  = sum(1 for v in instruments.values()
                     if "RANGE" in v.get("current_state","") or
                        "COMPRESSION" in v.get("current_state","")) / total
    green_n    = sum(1 for v in instruments.values() if v.get("_tier") == "GREEN")

    if green_n >= 3 or bull_pct >= 0.55 or bear_pct >= 0.55:
        return "Trending"
    if range_pct >= 0.50:
        return "Compressing"
    return "Ranging"


# ── Main briefing builder ─────────────────────────────────────────────────────

def build_briefing() -> str:
    now      = now_utc()
    sast_now = to_sast(now)
    today    = now.strftime("%Y-%m-%d")

    sast_str = sast_now.strftime("%d %b %Y %H:%M SAST")
    session  = current_session(now.hour)
    kz_str   = kill_zone_status(now)

    # ── Load ─────────────────────────────────────────────────────────────────
    live        = load_json(LIVE_STATE)
    instruments = live.get("instruments", {})
    log_rows    = load_signal_log_today(today)

    if not instruments:
        return "# H2 SESSION BRIEF\n\n⚠ No live state data. Run `python live/monitor.py --once` first.\n"

    # ── Score and classify ────────────────────────────────────────────────────
    ranked       = []
    avoid_rows   = []
    manual_rows  = []

    for inst, data in instruments.items():
        tf       = data.get("timeframe", "1H")
        state_id = data.get("current_state", "UNKNOWN")
        samples  = data.get("sample_count", 0)
        ruin     = data.get("ruin_pct", 0.0)
        score    = data.get("conviction_score") or conviction_score(data)
        tier     = assign_tier(data, score)
        data["_tier"] = tier          # stash for character calc

        priority = PRIORITY_ORDER.index(inst) if inst in PRIORITY_ORDER else 99
        sharpe   = SHARPE_WEIGHTS.get(inst, {}).get(tf, 1.0)
        next_states = data.get("next_states", [])
        top_next    = next_states[0] if next_states else {}

        row = {
            "inst":     inst,
            "tf":       tf,
            "state_id": state_id,
            "tier":     tier,
            "conv":     data.get("conviction", "SKIP"),
            "pillars":  data.get("pillars_confirmed", 0),
            "all_gates":data.get("all_gates_pass", False),
            "gates_n":  gates_count(data),
            "samples":  samples,
            "ruin":     ruin,
            "score":    score,
            "sharpe":   sharpe,
            "priority": priority,
            "wr":       data.get("historical_wr", 0.0),
            "ev":       data.get("historical_ev", 0.0),
            "top_next": top_next,
            "data":     data,
        }

        if inst in MANUAL_WATCH:
            manual_rows.append(row)
        elif inst in AVOID_ALWAYS or tier == "AVOID" or ruin > 5.0:
            avoid_rows.append(row)
        else:
            ranked.append(row)

    tier_order = {"GREEN": 0, "AMBER": 1, "RED": 2, "AVOID": 3}
    ranked.sort(key=lambda r: (
        tier_order.get(r["tier"], 4),
        -(r["score"] * r["sharpe"]),
        r["priority"],
    ))

    green_setups = [r for r in ranked if r["tier"] == "GREEN"]
    amber_setups = [r for r in ranked if r["tier"] == "AMBER"]
    top5         = (green_setups + amber_setups)[:5] or ranked[:5]

    character  = market_character(instruments)
    green_n    = len(green_setups)

    # ── Signal log stats ──────────────────────────────────────────────────────
    fired_today  = [r for r in log_rows if str(r.get("fired","")).lower() == "true"]
    failed_today = [r for r in log_rows
                    if r.get("outcome_r") and
                    str(r["outcome_r"]).replace("-","").replace(".","").isdigit() and
                    float(r["outcome_r"]) < 0]

    compression_list = [
        inst for inst, data in instruments.items()
        if "RANGE" in data.get("current_state","") or
           "COMPRESSION" in data.get("current_state","")
    ]

    # ── Avoid reasons ─────────────────────────────────────────────────────────
    avoid_reasons: dict = dict(AVOID_REASONS_PERMANENT)  # always include all 3
    for r in avoid_rows:
        if r["inst"] in avoid_reasons:
            continue
        parts = []
        if r["ruin"] > 5.0:
            parts.append(f"ruin {r['ruin']:.1f}% > 5% threshold")
        if r["samples"] < 30:
            parts.append(f"only {r['samples']} samples — below 30 minimum")
        if "RANGE" in r["state_id"] or "COMPRESSION" in r["state_id"]:
            parts.append("compression / range — no directional edge")
        if not parts:
            parts.append("conviction score below threshold")
        avoid_reasons[r["inst"]] = ", ".join(parts)

    for inst in compression_list:
        if inst not in avoid_reasons and inst not in ALL_EXCLUDED:
            avoid_reasons[inst] = "compression / range — no directional edge"

    # ══════════════════════════════════════════════════════════════════════════
    # BUILD MARKDOWN
    # ══════════════════════════════════════════════════════════════════════════
    L = []

    # ── SECTION 1 — SESSION HEADER ────────────────────────────────────────────
    L += [
        "# H2 SESSION BRIEF",
        f"**Generated: {sast_str}**",
        f"**Session: {session}**",
        f"**Kill zone: {kz_str}**",
        f"**Market character: {character}**",
        f"**GREEN setups available: {green_n}**",
        "",
        "---",
        "",
    ]

    # ── SECTION 2 — TOP 5 RIGHT NOW ───────────────────────────────────────────
    if green_n == 0:
        next_sess_sast = NEXT_SESSION_SAST.get(session, "next session open")
        L += [
            "## SECTION 2 — TOP 5 RIGHT NOW",
            "",
            "> **No A+ setups active — patience required.**",
            f"> Next opportunity: {next_sess_sast} SAST",
            "",
        ]
    else:
        L += ["## SECTION 2 — TOP 5 RIGHT NOW", ""]

    for rank, row in enumerate(top5, 1):
        inst     = row["inst"]
        tf       = row["tf"]
        sid      = row["state_id"]
        data     = row["data"]
        top_next = row["top_next"]
        sigs     = pillar_signals(sid, data)
        sit      = situation_text(inst, sid, data)

        sess_label  = data.get("session", "—")
        low_samples = row["samples"] < 30

        next_str = (f"`{top_next.get('state','—')}` — "
                    f"{top_next.get('probability',0)*100:.0f}% probability"
                    if top_next else "— insufficient data")

        wr_str   = f"{row['wr']*100:.1f}%" if row["wr"] > 0 else "—"
        ev_sign  = f"+{row['ev']:.2f}R" if row["ev"] >= 0 else f"{row['ev']:.2f}R"
        stats_line = (f"WR {wr_str} · EV {ev_sign} · {row['samples']} samples · "
                      f"Gates {row['gates_n']}/5 · Pillars {row['pillars']}/4")

        if low_samples:
            L.append(f"> ⚠ **Low sample count ({row['samples']} trades) — "
                     f"statistics unreliable. Treat as AMBER maximum.**")
            L.append("")

        L += [
            f"### {rank}. {inst} · {tf} · {row['conv']} · {sess_label}",
            f"**State:** `{sid}`",
            "",
            f"**Situation:** {sit}",
            "",
            "**Look for before entering:**",
            f"- Volume: {sigs['volume']}",
            f"- Structure: {sigs['structure']}",
            f"- Fractals: {sigs['fractals']}",
            f"- MTF: {sigs['mtf']}",
            "",
            f"**Do NOT enter if:** {sigs['invalid']}",
            "",
            f"**Stats:** {stats_line}",
            "",
            "---",
            "",
        ]

    # ── SECTION 3 — FULL INSTRUMENT TABLE ────────────────────────────────────
    L += [
        "## SECTION 3 — FULL INSTRUMENT TABLE",
        "",
        "| # | Tier | Instrument | TF | Current State | Next State | P% | EV | WR | Pillars | Score |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for i, row in enumerate(ranked, 1):
        tn    = row["top_next"]
        nxt   = tn.get("state", "—")[:30] if tn else "—"
        pct   = f"{tn.get('probability',0)*100:.0f}%" if tn else "—"
        ev_s  = f"+{row['ev']:.2f}R" if row["ev"] >= 0 else f"{row['ev']:.2f}R"
        wr_s  = f"{row['wr']*100:.0f}%" if row["wr"] > 0 else "—"
        icon  = TIER_ICON.get(row["tier"], "⚪")
        L.append(
            f"| {i} | {icon} | {row['inst']} | {row['tf']} "
            f"| {row['state_id'][:28]} | {nxt} "
            f"| {pct} | {ev_s} | {wr_s} | {row['pillars']}/4 | {row['score']:.2f} |"
        )
    L += ["", "---", ""]

    # ── SECTION 4 — AVOID LIST ────────────────────────────────────────────────
    L += ["## SECTION 4 — AVOID LIST", "", "| Instrument | Reason |", "|---|---|"]
    for inst in sorted(avoid_reasons):
        L.append(f"| ⛔ {inst} | {avoid_reasons[inst]} |")
    L += ["", "---", ""]

    # ── SECTION 5 — RISK FLAGS ────────────────────────────────────────────────
    comp_str = ", ".join(sorted(set(compression_list))) if compression_list else "None"
    L += [
        "## SECTION 5 — RISK FLAGS",
        "",
        "| Flag | Value |",
        "|---|---|",
        "| News next 2 hours | None identified — check ForexFactory manually before entry |",
        f"| Signals fired today | {len(fired_today)} |",
        f"| Failed today | {len(failed_today)} |",
        f"| In compression | {comp_str} |",
        "| Session note | — |",
        "",
        "---",
        "",
    ]

    # ── SECTION 6 — JP225 MANUAL WATCH ───────────────────────────────────────
    L += ["## SECTION 6 — JP225 MANUAL WATCH", ""]

    jp_data = None
    for row in manual_rows:
        if row["inst"] == "JP225":
            jp_data = row
            break

    if jp_data:
        d        = jp_data["data"]
        sid      = jp_data["state_id"]
        tn       = jp_data["top_next"]
        nxt_str  = (f"`{tn.get('state','—')}` — "
                    f"{tn.get('probability',0)*100:.0f}% probability · "
                    f"EV {tn.get('ev',0.0):+.2f}R"
                    if tn else "— insufficient data")
        L += [
            f"**State:** `{sid}`",
            f"**Next state forecast:** {nxt_str}",
            "",
            "> Manual discretion only. Systematic signals excluded — ruin 14%.",
            "> Revisit after data expansion (target: 200+ samples per state).",
            "",
        ]
    else:
        L += [
            "JP225 data not in current live state.",
            "",
            "> Manual discretion only. Systematic signals excluded — ruin 14%.",
            "> Revisit after data expansion (target: 200+ samples per state).",
            "",
        ]
    L += ["---", ""]

    # ── SIGNALS FIRED TABLE (bonus — not in spec but useful) ─────────────────
    L += [
        "## SIGNALS FIRED TODAY",
        "",
        "| SAST | Instrument | Dir | Conv | EV | Outcome |",
        "|---|---|---|---|---|---|",
    ]
    if fired_today:
        for r in fired_today:
            sast_t  = r.get("sast_time","")[:16]
            outcome = r.get("outcome_r","—") or "—"
            L.append(
                f"| {sast_t} | {r.get('instrument','')} | "
                f"{r.get('direction','')} | {r.get('conviction','')} | "
                f"{r.get('ev','—')} | {outcome} |"
            )
    else:
        L.append("| — | No signals fired today | — | — | — | — |")
    L += ["", "---", ""]

    # ── SECTION 7 — YOUR NEXT ACTION ─────────────────────────────────────────
    L += ["## SECTION 7 — YOUR NEXT ACTION", ""]

    best = top5[0] if top5 else None
    if best:
        inst     = best["inst"]
        tf       = best["tf"]
        sid      = best["state_id"]
        sigs     = pillar_signals(sid, best["data"])
        tn       = best["top_next"]
        tn_parts = (tn.get("state","") or "").split("_")
        tn_struct = tn_parts[5] if len(tn_parts) > 5 else "BOS"
        direction = "long" if sid.startswith("BULL") else "short"
        gate_note = "all 5 gates passing" if best["all_gates"] else f"{best['gates_n']}/5 gates passing"

        action = (
            f"Open **{inst}** on the **{tf}** chart. "
            f"The system is in a `{sid}` state with {tn_struct} forecast "
            f"as the next state ({gate_note}). "
            f"Wait for {sigs['structure'].lower()} and {sigs['volume'].lower()} "
            f"before entering {direction} — "
            f"do not enter until {sigs['invalid'].lower()}."
        )
    else:
        next_sess = NEXT_SESSION_SAST.get(session, "next session")
        action = (
            f"No qualified setups active at this time. "
            f"Monitor for state transitions at {next_sess} SAST. "
            f"Patience is a position."
        )

    L += [action, "", "---"]
    L.append(f"*H2 Quant System v1 · Generated {sast_str}*")

    return "\n".join(L)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    print("=" * 70)
    print("  H2 BRIEF GENERATOR v2.0")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 70)

    brief = build_briefing()

    BRIEF_OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(BRIEF_OUT, "w", encoding="utf-8") as f:
        f.write(brief)

    print(brief)
    print()
    print("=" * 70)
    print(f"  Saved -> {BRIEF_OUT}")
    print(f"  Prompt -> {ROOT / 'cowork' / 'H2_BRIEF_PROMPT.md'}")
    print("=" * 70)


if __name__ == "__main__":
    main()
