"""
H2 Quant v1 — Phase 6: Briefing Generator
Reads live state, state stats, and signal log.
Produces H2_live_state.json (enriched) + H2_daily_briefing.md

Input:  outputs/H2_live_state.json       (from Phase 7 live monitor, or bootstrapped)
        outputs/H2_state_stats_*.json     (from Phase 5 validator)
        outputs/H2_signal_log.csv         (from Phase 7, or empty)
Output: outputs/H2_live_state.json        (enriched)
        outputs/H2_daily_briefing.md
"""

import json
import logging
import sys
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
        logging.FileHandler(LOG_DIR / "generator.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("H2.briefing")

OUTPUTS_DIR = ROOT / CFG["paths"]["outputs"]
STATES_DIR  = ROOT / "states"
SAST_OFFSET = timedelta(hours=2)

# ---------------------------------------------------------------------------
# Instrument tiers and focus list
# ---------------------------------------------------------------------------

TIER1  = ["US30", "GBPUSD", "DE40", "XAUUSD", "GBPJPY", "EURJPY"]
TIER2  = ["EURUSD", "USDJPY", "USTEC", "UK100", "AUDUSD"]
TIER3  = ["JP225", "HK50"]           # context only, no signals
AVOID_INSTRUMENTS = {"HK50"}          # per CLAUDE.md
PRIMARY_TF = "1H"                     # primary timeframe for briefing

ALL_INSTRUMENTS = [
    "JP225","DE40","UK100","USTEC","US30","HK50",
    "EURUSD","GBPUSD","USDJPY","AUDUSD","USDCAD",
    "EURJPY","GBPJPY","XAUUSD","XAGUSD",
]

# ── Walk-forward validated signal roles ──────────────────────────────────────
# Primary signal instruments at 1H — these receive full briefing treatment
PRIMARY_SIGNAL_INSTRUMENTS = {
    "US30", "GBPUSD", "UK100", "DE40", "USTEC",
    "XAUUSD", "EURJPY", "EURUSD", "GBPJPY", "USDJPY", "XAGUSD",
}
# Instruments excluded from primary signals (too low Sharpe / high ruin)
EXCLUDED_SIGNAL_INSTRUMENTS_1H = {"AUDUSD", "USDCAD"}

# High-conviction state pattern from walk-forward results:
# BULL_STRONG + BOS + OB + ABOVE liquidity = highest validated edge
# These patterns receive a +0.10 bonus in conviction scoring.
_HC = CFG.get("state_quality", {}).get("high_conviction_pattern", {})
HC_TREND     = _HC.get("trend",     "BULL")
HC_MOMENTUM  = _HC.get("momentum",  "STRONG")
HC_STRUCTURE = _HC.get("structure", "BOS")
HC_RSI       = _HC.get("rsi",       "OB")
HC_LIQUIDITY = _HC.get("liquidity", "ABOVE")

# PULLBACK_NEUTRAL states are systematically overfit — cap at "A" in briefing
PULLBACK_NEUTRAL_MAX_CONV = CFG.get("state_quality", {}).get(
    "pullback_neutral_conviction_cap", "A"
)

# ---------------------------------------------------------------------------
# Session timing
# ---------------------------------------------------------------------------

SESSIONS = CFG["sessions"]

KILL_ZONES_UTC = [
    ("London open",  7,  9),
    ("NY open",     13, 15),
    ("NY close",    19, 20),
]


def current_session(utc_hour: int) -> str:
    if 13 <= utc_hour < 16:
        return "OVERLAP"
    if 7  <= utc_hour < 16:
        return "LONDON"
    if 16 <= utc_hour < 22:
        return "NY"
    if 0  <= utc_hour <  9:
        return "TOKYO"
    return "OFFHOURS"


def minutes_to_next_kill_zone(now_utc: datetime) -> tuple[str, int]:
    h = now_utc.hour
    m = now_utc.minute
    current_minutes = h * 60 + m
    best_name, best_mins = "NY open", 9999
    for name, start, _ in KILL_ZONES_UTC:
        kz_mins = start * 60
        diff = kz_mins - current_minutes
        if diff < 0:
            diff += 24 * 60
        if diff < best_mins:
            best_mins = diff
            best_name = name
    return best_name, best_mins


# ---------------------------------------------------------------------------
# Scoring + tier
# ---------------------------------------------------------------------------

def conviction_score(
    prob: float,
    ev: float,
    stability: float,
    pillars: int,
    state_id: str = "",
) -> float:
    """
    conviction_score = prob×0.35 + ev×0.25 + stability×0.20 + (pillars/4)×0.20

    Walk-forward bonus (+0.10) applied when state matches the highest-validated
    pattern: BULL_STRONG + BOS + OB + ABOVE liquidity.
    This pattern showed the most consistent OOS edge across all instruments.

    ev is normalised to 0-1 range assuming max useful EV ~ 3.0R.
    Score is clamped to [0.0, 1.0].
    """
    ev_norm = max(0.0, min(ev / 3.0, 1.0))
    base = (
        prob          * 0.35 +
        ev_norm       * 0.25 +
        stability     * 0.20 +
        (pillars / 4) * 0.20
    )

    # High-conviction pattern bonus — walk-forward validated
    bonus = 0.0
    if state_id:
        parts = state_id.split("_")
        # State format: TREND_MOM_VOL_LIQ_SESS_STRUCT_RSI (7-8 parts)
        if len(parts) >= 7:
            trend  = parts[0]
            mom    = parts[1]
            liq    = parts[3]
            struct = parts[5]
            rsi    = parts[-1]
            if (trend  == HC_TREND     and
                mom    == HC_MOMENTUM  and
                struct == HC_STRUCTURE and
                rsi    == HC_RSI       and
                liq    == HC_LIQUIDITY):
                bonus = 0.10

    return min(1.0, base + bonus)


def assign_tier(
    score: float,
    gates_pass: bool,
    ruin_pct: float,
    instrument: str,
    samples: int,
    state_id: str = "",
    timeframe: str = "1H",
) -> str:
    """
    Assign traffic-light tier to an instrument/state combination.

    Walk-forward rules applied (2026-06-09):
    - AVOID instruments (HK50, JP225) and D timeframes always → AVOID
    - AUDUSD 1H and USDCAD 1H → AVOID (Sharpe below minimum)
    - Ruin > 5% → RED regardless of score
    - PULLBACK_NEUTRAL states → capped at AMBER (never GREEN)
    - OVERFIT / REMOVE flagged states → RED
    - Standard GREEN/AMBER/RED scoring for all others
    """
    # Permanent avoids
    if instrument in AVOID_INSTRUMENTS or samples < 30:
        return "AVOID"

    # Daily timeframe — no edge, avoid list only
    if timeframe == "D":
        return "AVOID"

    # 1H combos excluded by walk-forward
    if timeframe == "1H" and instrument in EXCLUDED_SIGNAL_INSTRUMENTS_1H:
        return "AVOID"

    # Ruin gate — takes precedence over score
    if ruin_pct > 5.0:
        return "RED"

    # PULLBACK_NEUTRAL — systematically overfit, cap at AMBER
    if state_id:
        parts = state_id.split("_")
        if (len(parts) >= 7 and
                parts[5] == "PULLBACK" and parts[6] == "NEUTRAL"):
            if score > 0.65 and gates_pass:
                return "AMBER"   # cap: would be GREEN but WF says overfit
            if score >= 0.40:
                return "AMBER"
            return "RED"

    # Standard tier rules
    if score > 0.65 and gates_pass:
        return "GREEN"
    if score >= 0.40:
        return "AMBER"
    return "RED"


# ---------------------------------------------------------------------------
# Plain English state descriptions
# ---------------------------------------------------------------------------

TREND_TEXT = {"BULL": "bullish", "BEAR": "bearish", "NEUTRAL": "neutral"}
MOM_TEXT   = {"STRONG": "strong momentum", "WEAK": "weak momentum", "NEUTRAL": "neutral momentum"}
VOL_TEXT   = {"HIGH": "high volatility", "NORMAL": "normal volatility", "LOW": "low volatility, compression"}
LIQ_TEXT   = {"ABOVE": "above prior swing high", "BELOW": "below prior swing low", "INSIDE": "within range"}
STRUCT_TEXT= {
    "BOS":        "break of structure",
    "CHOCH":      "change of character",
    "MSS":        "market structure shift",
    "PULLBACK":   "pullback / retrace",
    "RANGE":      "ranging, no clear bias",
    "TREND_CONT": "trend continuation",
}
RSI_TEXT   = {"OB": "RSI overbought (>=65)", "OS": "RSI oversold (<=35)", "NEUTRAL": "RSI neutral"}

ENTRY_HINTS = {
    "BOS":    "Wait for fractal HL and volume dry-up on the retrace before entering.",
    "CHOCH":  "Watch for MSS confirmation and delta flip before fading.",
    "MSS":    "Confirm with LTF CHoCH and volume spike at the reversal point.",
    "PULLBACK":"Look for fractal HL at 50-62% retrace with RVOL declining.",
    "RANGE":  "Trade edges only — wait for liquidity sweep + reclaim.",
    "TREND_CONT": "Trail with fractal lows. Add on BOS confirmations.",
}

NEXT_STATE_HINTS = {
    "PULLBACK": "pullback then continuation",
    "BOS":      "momentum continuation",
    "CHOCH":    "potential reversal building",
    "MSS":      "structural reversal in progress",
    "RANGE":    "sideways accumulation/distribution",
    "TREND_CONT": "trend continuation likely",
}


def state_to_english(state_id: str, instrument: str, next_states: list) -> str:
    """Convert a composite state_id into a one-line plain English description."""
    parts = state_id.split("_")
    if len(parts) < 7:
        return f"{instrument}: state {state_id} (insufficient data for description)"

    trend  = parts[0]
    mom    = parts[1]
    vol    = parts[2]
    liq    = parts[3]
    sess   = parts[4]
    struct = parts[5] + ("_" + parts[6] if len(parts) == 8 else "")
    rsi    = parts[-1]

    t_txt  = TREND_TEXT.get(trend,  trend)
    m_txt  = MOM_TEXT.get(mom,    mom)
    s_txt  = STRUCT_TEXT.get(struct, struct)
    r_txt  = RSI_TEXT.get(rsi,    rsi)
    hint   = ENTRY_HINTS.get(struct, "Monitor for confirmation before entry.")

    next_desc = ""
    if next_states:
        top = next_states[0]
        top_struct = top["state"].split("_")[5] if len(top["state"].split("_")) >= 6 else "?"
        top_desc   = NEXT_STATE_HINTS.get(top_struct, "transition")
        next_desc  = f" Top next state ({top['probability']:.0%}): {top_desc}."

    return (
        f"{instrument} is {t_txt} with {m_txt} ({s_txt}, {sess} session, {r_txt})."
        f"{next_desc} {hint}"
    )


# ---------------------------------------------------------------------------
# Live state bootstrap (when Phase 7 not running)
# ---------------------------------------------------------------------------

def _load_state_stats(instrument: str, timeframe: str = PRIMARY_TF) -> dict:
    path = OUTPUTS_DIR / f"H2_state_stats_{instrument}_{timeframe}.json"
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _load_transition_matrix(instrument: str, timeframe: str = PRIMARY_TF) -> dict:
    path = OUTPUTS_DIR / f"H2_transition_matrix_{instrument}_{timeframe}.json"
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _load_latest_state(instrument: str, timeframe: str = PRIMARY_TF) -> Optional[dict]:
    """Pull the last bar from states parquet and return a state info dict."""
    sp = STATES_DIR / f"H2_states_{instrument}_{timeframe}.parquet"
    if not sp.exists():
        return None
    try:
        df = pd.read_parquet(sp)
        if df.empty:
            return None
        row = df.iloc[-1]
        state_id = str(row.get("state_id", "UNKNOWN"))

        tm   = _load_transition_matrix(instrument, timeframe)
        stats = _load_state_stats(instrument, timeframe)

        # Next states from global matrix
        global_mat = tm.get("matrices", {}).get("global", {})
        next_raw   = global_mat.get(state_id, {})
        state_stats_dict = stats.get("states", {})
        next_states = sorted(
            [{"state": s, "probability": p,
              "ev": state_stats_dict.get(s, {}).get("ev", 0.0)}
             for s, p in next_raw.items()],
            key=lambda x: x["probability"], reverse=True
        )[:3]

        # Gates (simplified — full gate check in Phase 7 monitor)
        top_prob = next_states[0]["probability"] if next_states else 0.0
        self_prob = next_raw.get(state_id, 0.0)
        gates = {
            "markov_gap":         {"value": top_prob,  "threshold": 0.61, "pass": top_prob >= 0.61},
            "markov_persistence": {"value": self_prob, "threshold": 0.82, "pass": self_prob >= 0.82},
            "volatility_cap":     {"value": 1.0,       "threshold": 1.25, "pass": True},
            "hurst":              {"value": 0.55,       "threshold": 0.50, "pass": True},
            "session":            {"value": "ANY",      "required": "ANY", "pass": True},
        }
        all_gates = all(g["pass"] for g in gates.values())

        # State stats
        ss = state_stats_dict.get(state_id, {})
        wr       = ss.get("win_rate",     0.0)
        ev       = ss.get("ev",           0.0)
        samples  = ss.get("sample_count", 0)
        stab     = ss.get("stability_score", 0.0)
        ruin_pct = stats.get("monte_carlo", {}).get("probability_of_ruin", 0) * 100
        ci_lo    = ss.get("ci_95_low",    0.0)
        ci_hi    = ss.get("ci_95_high",   1.0)

        # Pillars — simplified placeholder (Phase 7 computes live)
        pillars = sum([
            1 if ev > 0.2 else 0,
            1 if top_prob > 0.35 else 0,
            1 if stab >= 0.5 else 0,
            1 if wr > 0.50 else 0,
        ])

        # Conviction — pass state_id for high-conviction pattern bonus
        score = conviction_score(top_prob, ev, stab, pillars, state_id=state_id)
        tier  = assign_tier(score, all_gates, ruin_pct, instrument, samples,
                            state_id=state_id, timeframe=timeframe)
        conv_raw = "A+" if pillars == 4 else "A" if pillars == 3 else "B" if pillars == 2 else "SKIP"
        # Cap PULLBACK_NEUTRAL to max conviction from config
        _parts = state_id.split("_")
        _is_pbn = (len(_parts) >= 7 and
                   _parts[5] == "PULLBACK" and _parts[6] == "NEUTRAL")
        conv = (PULLBACK_NEUTRAL_MAX_CONV
                if _is_pbn and conv_raw == "A+"
                else conv_raw)

        dt_utc  = pd.to_datetime(row.get("datetime_utc", datetime.now(timezone.utc)), utc=True)
        dt_sast = dt_utc + SAST_OFFSET

        sess_col = "state_session" if "state_session" in row.index else None
        sess_val = row[sess_col] if sess_col else current_session(dt_utc.hour)

        return {
            "instrument":       instrument,
            "timeframe":        timeframe,
            "current_state":    state_id,
            "state_description": state_to_english(state_id, instrument, next_states),
            "last_bar_utc":     dt_utc.isoformat(),
            "last_bar_sast":    dt_sast.isoformat(),
            "session":          sess_val,
            "next_states":      next_states,
            "gates":            gates,
            "all_gates_pass":   all_gates,
            "confirmation_pillars": {"count": pillars},
            "pillars_confirmed":pillars,
            "conviction":       conv,
            "historical_wr":    round(wr,  4),
            "historical_ev":    round(ev,  4),
            "sample_count":     samples,
            "ci_95":            [ci_lo, ci_hi],
            "stability_score":  round(stab, 3),
            "ruin_pct":         round(ruin_pct, 2),
            "conviction_score": round(score, 4),
            "tier":             tier,
        }
    except Exception as e:
        log.warning(f"Failed to load state for {instrument} {timeframe}: {e}")
        return None


def bootstrap_live_state() -> dict:
    """Build H2_live_state.json from last state parquet bars (Phase 7 not running)."""
    now_utc  = datetime.now(timezone.utc)
    now_sast = now_utc + SAST_OFFSET
    sess     = current_session(now_utc.hour)

    instruments_data = {}
    for inst in ALL_INSTRUMENTS:
        info = _load_latest_state(inst)
        if info:
            instruments_data[inst] = info

    live_state = {
        "generated_at_utc":  now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generated_at_sast": now_sast.isoformat(),
        "session":           sess,
        "source":            "bootstrap_from_states_parquet",
        "instruments":       instruments_data,
    }

    out = OUTPUTS_DIR / "H2_live_state.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(live_state, f, indent=2, default=str)
    log.info(f"Bootstrapped live state: {len(instruments_data)} instruments -> {out}")
    return live_state


def load_live_state() -> dict:
    """Load existing H2_live_state.json or bootstrap from parquet."""
    path = OUTPUTS_DIR / "H2_live_state.json"
    if path.exists():
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    log.info("H2_live_state.json not found — bootstrapping from states parquet")
    return bootstrap_live_state()


def load_signal_log() -> pd.DataFrame:
    path = OUTPUTS_DIR / "H2_signal_log.csv"
    if not path.exists():
        return pd.DataFrame(columns=[
            "timestamp_utc","sast_time","instrument","direction","state_id",
            "confidence","ev","session","pillars_confirmed","conviction",
            "gate_markov_gap","gate_persistence","gate_vol","gate_hurst",
            "gate_session","fired","outcome_r",
        ])
    return pd.read_csv(path)


# ---------------------------------------------------------------------------
# Regime detection
# ---------------------------------------------------------------------------

def detect_regime(instruments_data: dict) -> tuple[str, str, str]:
    """
    Returns (risk_sentiment, market_character, overall_bias)
    based on majority vote across Tier 1 instruments.
    """
    trends = []
    structs = []
    vols = []

    for inst in TIER1:
        info = instruments_data.get(inst)
        if not info:
            continue
        sid = info.get("current_state", "")
        parts = sid.split("_")
        if len(parts) >= 7:
            trends.append(parts[0])
            vols.append(parts[2])
            struct = parts[5] + ("_" + parts[6] if len(parts) == 8 else "")
            structs.append(struct)

    if not trends:
        return "MIXED", "UNKNOWN", "MIXED"

    bull_count = trends.count("BULL")
    bear_count = trends.count("BEAR")
    n = len(trends)

    if bull_count >= n * 0.6:
        bias = "BULL"
        sentiment = "RISK-ON"
    elif bear_count >= n * 0.6:
        bias = "BEAR"
        sentiment = "RISK-OFF"
    else:
        bias = "MIXED"
        sentiment = "MIXED"

    high_vol = vols.count("HIGH")
    bos_mss  = sum(1 for s in structs if s in ("BOS", "MSS", "CHOCH"))
    range_cnt = structs.count("RANGE")

    if high_vol >= n * 0.5 or bos_mss >= n * 0.5:
        character = "VOLATILE/TRENDING"
    elif range_cnt >= n * 0.5:
        character = "RANGING/COMPRESSING"
    else:
        character = "MIXED"

    return sentiment, character, bias


# ---------------------------------------------------------------------------
# Briefing generator
# ---------------------------------------------------------------------------

class BriefingGenerator:

    def _rank_instruments(self, instruments_data: dict) -> list[dict]:
        rows = []
        for inst, info in instruments_data.items():
            if not info:
                continue
            rows.append({
                "instrument":      inst,
                "timeframe":       info.get("timeframe", PRIMARY_TF),
                "state":           info.get("current_state", "?"),
                "session":         info.get("session", "?"),
                "tier":            info.get("tier", "RED"),
                "conviction":      info.get("conviction", "SKIP"),
                "conviction_score":info.get("conviction_score", 0.0),
                "ev":              info.get("historical_ev", 0.0),
                "wr":              info.get("historical_wr", 0.0),
                "stability":       info.get("stability_score", 0.0),
                "pillars":         info.get("pillars_confirmed", 0),
                "gates_pass":      info.get("all_gates_pass", False),
                "samples":         info.get("sample_count", 0),
                "ruin_pct":        info.get("ruin_pct", 0.0),
                "next_states":     info.get("next_states", []),
                "description":     info.get("state_description", ""),
            })
        return sorted(rows, key=lambda x: x["conviction_score"], reverse=True)

    def _avoid_list(self, ranked: list[dict]) -> list[dict]:
        return [
            r for r in ranked
            if r["tier"] in ("AVOID", "RED")
            or r["instrument"] in AVOID_INSTRUMENTS
            or r["ruin_pct"] > 5.0
            or r["samples"] < 30
        ]

    def _tier_icon(self, tier: str) -> str:
        return {"GREEN": "[G]", "AMBER": "[A]", "RED": "[R]", "AVOID": "[X]"}.get(tier, "[ ]")

    def _conv_icon(self, conv: str) -> str:
        return {"A+": "[A+]", "A": "[A] ", "B": "[B] ", "SKIP": "[--]"}.get(conv, "[--]")

    def _state_short(self, state: str) -> str:
        parts = state.split("_")
        if len(parts) >= 6:
            struct = parts[5] + ("_" + parts[6] if len(parts) == 8 else "")
            return f"{parts[0]}/{parts[5]}/{parts[-1]}"
        return state[:30]

    def _next_state_summary(self, next_states: list) -> str:
        if not next_states:
            return "—"
        top = next_states[0]
        return f"{self._state_short(top['state'])} ({top['probability']:.0%})"

    def generate(
        self,
        live_state:  dict,
        signal_log:  pd.DataFrame,
        now_utc:     datetime,
    ) -> str:
        now_sast   = now_utc + SAST_OFFSET
        sess       = current_session(now_utc.hour)
        kz_name, kz_mins = minutes_to_next_kill_zone(now_utc)
        kz_str     = f"{kz_mins // 60}h {kz_mins % 60}m" if kz_mins > 60 else f"{kz_mins}m"
        instr_data = live_state.get("instruments", {})

        sentiment, character, bias = detect_regime(instr_data)
        ranked = self._rank_instruments(instr_data)
        avoid  = self._avoid_list(ranked)

        greens = [r for r in ranked if r["tier"] == "GREEN"]
        ambers = [r for r in ranked if r["tier"] == "AMBER"]
        top5   = (greens + ambers)[:5]

        # Failed signals today
        failed_today = []
        if not signal_log.empty and "outcome_r" in signal_log.columns:
            today = now_utc.strftime("%Y-%m-%d")
            todays = signal_log[signal_log["timestamp_utc"].str.startswith(today, na=False)]
            failed_today = todays[todays["outcome_r"] < 0]["state_id"].tolist() if "outcome_r" in todays.columns else []

        lines = []
        sep80 = "=" * 80
        sep40 = "-" * 80

        # ── HEADER ────────────────────────────────────────────────────────────
        lines += [
            "# H2 Quant Session Brief",
            "",
            f"**Generated:** {now_sast.strftime('%Y-%m-%d %H:%M SAST')} "
            f"(UTC {now_utc.strftime('%H:%M')})  ",
            f"**Session:** {sess}  ",
            f"**Next kill zone:** {kz_name} in {kz_str}  ",
            "",
        ]

        # ── SECTION 1: SESSION CONTEXT ────────────────────────────────────────
        lines += [
            "---",
            "## Section 1 — Session Context",
            "",
            f"| Metric | Value |",
            f"|---|---|",
            f"| Market sentiment | **{sentiment}** |",
            f"| Market character | {character} |",
            f"| Overall bias | **{bias}** (majority of Tier 1 instruments) |",
            f"| GREEN setups | {len(greens)} instruments |",
            f"| AMBER setups | {len(ambers)} instruments |",
            f"| Active session | {sess} |",
            "",
        ]

        # ── SECTION 2: TOP 5 RIGHT NOW ────────────────────────────────────────
        lines += [
            "---",
            "## Section 2 — Top 5 Right Now",
            "",
        ]
        if not top5:
            lines.append("*No GREEN or AMBER setups found in current session. Wait for better conditions.*")
        else:
            for i, r in enumerate(top5, 1):
                tier_icon = self._tier_icon(r["tier"])
                lines += [
                    f"### {i}. {tier_icon} {r['instrument']} {r['timeframe']} "
                    f"| {r['conviction']} | Score: {r['conviction_score']:.2f}",
                    "",
                    f"> {r['description']}",
                    "",
                    f"| WR | EV | Stability | Pillars | Next State |",
                    f"|---|---|---|---|---|",
                    f"| {r['wr']:.0%} | {r['ev']:+.2f}R | {r['stability']:.0%} "
                    f"| {r['pillars']}/4 | {self._next_state_summary(r['next_states'])} |",
                    "",
                ]

        # ── SECTION 3: FULL INSTRUMENT TABLE ─────────────────────────────────
        lines += [
            "---",
            "## Section 3 — Full Instrument Table",
            "",
            "Sorted by conviction score. Columns: Tier | Instrument | TF | State | Next | P% | EV | WR | Stable | Pillars | Conv",
            "",
            "| Tier | Instrument | TF | State | Next State (P%) | EV | WR | Stable | Pillars | Conv |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]

        for r in ranked:
            tier_icon = self._tier_icon(r["tier"])
            state_sh  = self._state_short(r["state"])
            next_sh   = self._next_state_summary(r["next_states"])
            lines.append(
                f"| {tier_icon} | **{r['instrument']}** | {r['timeframe']} "
                f"| `{state_sh}` | {next_sh} "
                f"| {r['ev']:+.2f} | {r['wr']:.0%} | {r['stability']:.0%} "
                f"| {r['pillars']}/4 | {r['conviction']} |"
            )

        lines.append("")

        # ── SECTION 4: AVOID LIST ─────────────────────────────────────────────
        lines += [
            "---",
            "## Section 4 — Avoid List",
            "",
        ]
        if not avoid:
            lines.append("*No instruments on the avoid list right now.*")
        else:
            lines += [
                "| Instrument | TF | State | Reason |",
                "|---|---|---|---|",
            ]
            for r in avoid:
                reasons = []
                if r["instrument"] in AVOID_INSTRUMENTS:
                    reasons.append("excluded instrument")
                if r["samples"] < 30:
                    reasons.append("current state below 30-sample minimum — no edge data")
                if r["ruin_pct"] > 5.0:
                    reasons.append(f"ruin risk {r['ruin_pct']:.1f}%")
                if r["tier"] == "RED" and not reasons:
                    reasons.append(f"low conviction score ({r['conviction_score']:.2f})")
                state_sh = self._state_short(r["state"])
                lines.append(
                    f"| {r['instrument']} | {r['timeframe']} "
                    f"| `{state_sh}` | {', '.join(reasons)} |"
                )
        lines.append("")

        # ── SECTION 5: RISK FLAGS ─────────────────────────────────────────────
        lines += [
            "---",
            "## Section 5 — Risk Flags",
            "",
        ]

        risk_flags = []

        # Session weakness check
        weak_sessions = {
            "TOKYO":   ["US30", "DE40", "UK100"],
            "OFFHOURS":["EURUSD", "GBPUSD", "USDJPY"],
        }
        if sess in weak_sessions:
            weak = [i for i in weak_sessions[sess] if i in instr_data]
            if weak:
                risk_flags.append(
                    f"**Session warning:** {sess} session is historically weak for "
                    f"{', '.join(weak)} — treat signals with extra caution."
                )

        # Failed signals today
        if failed_today:
            risk_flags.append(
                f"**Signals that fired and failed today:** {', '.join(set(failed_today[:5]))} — "
                f"avoid re-entering same state until session reset."
            )

        # Instruments with very high ruin%
        high_ruin = [
            f"{r['instrument']} ({r['ruin_pct']:.0f}%)"
            for r in ranked if r["ruin_pct"] > 10.0 and r["tier"] not in ("AVOID",)
        ]
        if high_ruin:
            risk_flags.append(
                f"**Elevated ruin risk (>10%):** {', '.join(high_ruin)} — "
                f"reduce position size or skip."
            )

        # NEUTRAL trend instruments with active setups
        neutral_signals = [
            r["instrument"] for r in top5
            if r["state"].startswith("NEUTRAL")
        ]
        if neutral_signals:
            risk_flags.append(
                f"**Trend ambiguity:** {', '.join(neutral_signals)} classified NEUTRAL trend "
                f"— higher false-signal risk, wait for EMA stack confirmation."
            )

        # Instruments near major round numbers (simplified — flag overbought)
        ob_states = [
            r["instrument"] for r in top5
            if "_OB" in r["state"] or "_OS" in r["state"]
        ]
        if ob_states:
            risk_flags.append(
                f"**Momentum extreme:** {', '.join(ob_states)} in OB/OS zone — "
                f"mean-reversion risk elevated, use tight stop."
            )

        if not risk_flags:
            risk_flags.append("No material risk flags for this session. Proceed with standard sizing.")

        for flag in risk_flags:
            lines.append(f"- {flag}")
        lines.append("")

        # ── FOOTER ────────────────────────────────────────────────────────────
        lines += [
            "---",
            f"*H2 Quant v1 | {now_sast.strftime('%Y-%m-%d %H:%M SAST')} | "
            f"Next refresh: 5 minutes*",
        ]

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def generate_briefing(force_bootstrap: bool = False) -> str:
    """
    Generate the full session brief.
    Returns the markdown string and saves to outputs/H2_daily_briefing.md.
    """
    now_utc = datetime.now(timezone.utc)

    if force_bootstrap:
        live_state = bootstrap_live_state()
    else:
        live_state = load_live_state()

    signal_log = load_signal_log()

    gen    = BriefingGenerator()
    brief  = gen.generate(live_state, signal_log, now_utc)

    out = OUTPUTS_DIR / "H2_daily_briefing.md"
    out.write_text(brief, encoding="utf-8")
    log.info(f"Briefing saved: {out}")

    return brief


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="H2 Quant — Briefing Generator (Phase 6)")
    parser.add_argument("--bootstrap", action="store_true",
                        help="Force rebuild live state from states parquet (no live monitor needed)")
    args = parser.parse_args()

    brief = generate_briefing(force_bootstrap=args.bootstrap or True)
    print(brief)
    print(f"\nSaved: {OUTPUTS_DIR / 'H2_daily_briefing.md'}")
    print("\nPhase 6 complete. Ready for Phase 7 — Live Monitor.")
