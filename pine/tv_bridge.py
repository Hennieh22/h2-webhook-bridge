"""
H2 Quant v1 — TradingView Bridge  (pine/tv_bridge.py)
======================================================
Reads outputs/H2_live_state.json every 5 minutes.
For each Tier 1 instrument, extracts all values needed
by the Pine Script overlay inputs and prints a status line.

Field mapping (H2_live_state.json  →  Pine Script input):
  current_state                    → i_state
  state_description                → i_description
  session (top-level)              → i_session
  generated_at_sast HH:MM          → i_sast_time
  dwell_time_current_bars          → i_dwell_bars
  dwell_time_avg_bars              → i_dwell_avg
  next_states[0].state             → i_next1
  next_states[0].probability       → i_prob1
  next_states[0].ev                → i_ev1
  next_states[1].state             → i_next2
  next_states[1].probability       → i_prob2
  next_states[1].ev                → i_ev2
  next_states[2].state             → i_next3
  next_states[2].probability       → i_prob3
  next_states[2].ev                → i_ev3
  gates.markov_gap.value           → i_g_markov_gap
  gates.markov_persistence.value   → i_g_persistence
  gates.volatility_cap.value       → i_g_vol_cap
  gates.hurst.value                → i_g_hurst
  gates.session.pass               → i_g_session_ok
  conviction                       → i_conviction
  pillars_confirmed                → i_pillars
  historical_wr                    → i_hist_wr
  historical_ev                    → i_hist_ev
  sample_count                     → i_samples

Usage:
  py -3 pine/tv_bridge.py                    # run once, all tier1
  py -3 pine/tv_bridge.py --loop             # 5-minute continuous loop
  py -3 pine/tv_bridge.py --verbose          # include full paste-values block
  py -3 pine/tv_bridge.py --instrument US30  # single instrument
"""

import io
import json
import sys
import time
import argparse
import logging
from pathlib import Path

import yaml

# ── Force UTF-8 stdout on Windows (avoids cp1252 encode errors) ────────────
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# ── Paths ───────────────────────────────────────────────────────────────────
ROOT        = Path(__file__).resolve().parent.parent
LIVE_STATE  = ROOT / "outputs" / "H2_live_state.json"
CONFIG_PATH = ROOT / "config.yaml"

# ── Config ──────────────────────────────────────────────────────────────────
with open(CONFIG_PATH, encoding="utf-8") as _f:
    CFG = yaml.safe_load(_f)

TIER1_INSTRUMENTS = CFG["instruments"]["tier1"]           # from config — never hardcoded
WEBHOOK_URL       = CFG.get("webhook", {}).get("railway_url",    "")
WEBHOOK_SECRET    = CFG.get("webhook", {}).get("webhook_secret", "")
LOOP_INTERVAL     = int(CFG.get("monitor", {}).get("interval_seconds", 300))

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("tv_bridge")


# ───────────────────────────────────────────────────────────────────────────
# 1.  Load live state JSON
# ───────────────────────────────────────────────────────────────────────────

def load_live_state() -> dict | None:
    """
    Load H2_live_state.json.
    Returns None (and logs) if the file is missing or malformed.
    """
    if not LIVE_STATE.exists():
        log.warning(
            "H2_live_state.json not found at %s -- waiting for monitor to run",
            LIVE_STATE,
        )
        return None
    try:
        with open(LIVE_STATE, encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        log.error("H2_live_state.json is malformed: %s", e)
        return None


# ───────────────────────────────────────────────────────────────────────────
# 2.  Extract Pine values for one instrument
# ───────────────────────────────────────────────────────────────────────────

def extract_pine_values(inst_data: dict, sast_timestamp: str) -> dict:
    """
    Map every field from one instrument's live-state dict to its
    Pine Script input variable name.

    sast_timestamp: top-level generated_at_sast string from the JSON,
                    e.g. "2026-06-09T10:32:55+02:00"  ->  i_sast_time = "10:32"
    """
    # i_sast_time: parsed from JSON timestamp — NOT datetime.now()
    try:
        sast_time = sast_timestamp[11:16]   # "2026-06-09T10:32:55+02:00" -> "10:32"
    except (TypeError, IndexError):
        sast_time = "--:--"

    # Next states (up to 3)
    next_states = inst_data.get("next_states", [])
    ns = [next_states[i] if i < len(next_states) else {} for i in range(3)]

    # Gates
    gates  = inst_data.get("gates", {})
    g_gap  = float(gates.get("markov_gap",         {}).get("value", 0.0))
    g_pers = float(gates.get("markov_persistence",  {}).get("value", 0.0))
    g_vol  = float(gates.get("volatility_cap",      {}).get("value", 1.0))
    g_hrst = float(gates.get("hurst",               {}).get("value", 0.5))
    g_sess = bool( gates.get("session",             {}).get("pass",  False))

    return {
        # Current state
        "i_state":       inst_data.get("current_state",    "UNKNOWN"),
        "i_description": (inst_data.get("state_description") or "")[:120],
        "i_session":     inst_data.get("session",          "OFFHOURS"),
        "i_sast_time":   sast_time,
        "i_dwell_bars":  int(  inst_data.get("dwell_time_current_bars") or 1),
        "i_dwell_avg":   round(float(inst_data.get("dwell_time_avg_bars")  or 0.0), 1),

        # Next-state transitions
        "i_next1": ns[0].get("state",       ""),
        "i_prob1": round(float(ns[0].get("probability", 0.0)), 4),
        "i_ev1":   round(float(ns[0].get("ev",          0.0)), 3),

        "i_next2": ns[1].get("state",       ""),
        "i_prob2": round(float(ns[1].get("probability", 0.0)), 4),
        "i_ev2":   round(float(ns[1].get("ev",          0.0)), 3),

        "i_next3": ns[2].get("state",       ""),
        "i_prob3": round(float(ns[2].get("probability", 0.0)), 4),
        "i_ev3":   round(float(ns[2].get("ev",          0.0)), 3),

        # Gate values
        "i_g_markov_gap":  round(g_gap,  4),
        "i_g_persistence": round(g_pers, 4),
        "i_g_vol_cap":     round(g_vol,  4),
        "i_g_hurst":       round(g_hrst, 4),
        "i_g_session_ok":  g_sess,

        # Conviction & stats
        "i_conviction": inst_data.get("conviction",      "SKIP"),
        "i_pillars":    int(  inst_data.get("pillars_confirmed", 0)),
        "i_hist_wr":    round(float(inst_data.get("historical_wr", 0.0)), 4),
        "i_hist_ev":    round(float(inst_data.get("historical_ev", 0.0)), 4),
        "i_samples":    int(  inst_data.get("sample_count",      0)),
    }


# ───────────────────────────────────────────────────────────────────────────
# 3.  Status line printer
# ───────────────────────────────────────────────────────────────────────────

def print_status(instrument: str, v: dict):
    """
    Print the compact one-liner per spec:
      TV bridge -- US30     updated 10:32 SAST | state | conv=A+ pillars=4/4 GATES OK | next=68% +1.80R
    """
    all_gates = (
        v["i_g_markov_gap"]  >= 0.61 and
        v["i_g_persistence"] >= 0.82 and
        v["i_g_vol_cap"]     <= 1.25 and
        v["i_g_hurst"]       >= 0.50 and
        v["i_g_session_ok"]
    )
    gates_str = "GATES OK" if all_gates else "gates FAIL"
    prob1_pct = int(round(v["i_prob1"] * 100))
    ev1_str   = f"{v['i_ev1']:+.2f}R"
    state_str = v["i_state"][:48]

    print(
        f"TV bridge -- {instrument:<8} updated {v['i_sast_time']} SAST  |  "
        f"{state_str:<48}  |  "
        f"conv={v['i_conviction']}  pillars={v['i_pillars']}/4  {gates_str}  |  "
        f"next={prob1_pct}% {ev1_str}"
    )


# ───────────────────────────────────────────────────────────────────────────
# 4.  Verbose paste-values block
# ───────────────────────────────────────────────────────────────────────────

def print_paste_values(instrument: str, v: dict):
    """Print every Pine input value formatted for manual entry in TV settings."""
    sep = "-" * 72
    print(f"\n{sep}")
    print(f"  PASTE VALUES FOR: {instrument}")
    print(sep)

    print(f"\n  -- Current State --")
    print(f"  i_state        : {v['i_state']}")
    print(f"  i_description  : {v['i_description']}")
    print(f"  i_session      : {v['i_session']}")
    print(f"  i_sast_time    : {v['i_sast_time']}")
    print(f"  i_dwell_bars   : {v['i_dwell_bars']}")
    print(f"  i_dwell_avg    : {v['i_dwell_avg']}")

    print(f"\n  -- Next States --")
    for n in (1, 2, 3):
        st  = v[f"i_next{n}"] or "(none)"
        pct = int(round(v[f"i_prob{n}"] * 100))
        ev  = v[f"i_ev{n}"]
        print(f"  [{n}] {pct:3d}%  {ev:+.3f}R  {st}")

    print(f"\n  -- Gates --")
    gate_rows = [
        ("i_g_markov_gap",  v["i_g_markov_gap"],  ">=", 0.61),
        ("i_g_persistence", v["i_g_persistence"],  ">=", 0.82),
        ("i_g_vol_cap",     v["i_g_vol_cap"],      "<=", 1.25),
        ("i_g_hurst",       v["i_g_hurst"],        ">=", 0.50),
    ]
    for name, val, op, thr in gate_rows:
        ok     = (val >= thr) if op == ">=" else (val <= thr)
        status = "PASS" if ok else "FAIL"
        print(f"  {name:<22} : {val}   {status}  (thr {op} {thr})")
    sess_ok = "PASS" if v["i_g_session_ok"] else "FAIL"
    print(f"  {'i_g_session_ok':<22} : {v['i_g_session_ok']}   {sess_ok}")

    print(f"\n  -- Conviction & Stats --")
    print(f"  i_conviction   : {v['i_conviction']}")
    print(f"  i_pillars      : {v['i_pillars']}/4")
    print(f"  i_hist_wr      : {v['i_hist_wr']}  ({v['i_hist_wr']*100:.1f}%)")
    print(f"  i_hist_ev      : {v['i_hist_ev']:+.4f}R")
    print(f"  i_samples      : {v['i_samples']}")
    print(f"{sep}\n")


# ───────────────────────────────────────────────────────────────────────────
# 5.  Update one instrument
# ───────────────────────────────────────────────────────────────────────────

def update_instrument(
    instrument: str,
    live_state: dict,
    verbose: bool = False,
) -> bool:
    """
    Extract and display Pine values for one instrument.
    Returns True on success, False if instrument absent from JSON.
    """
    inst_data = live_state.get("instruments", {}).get(instrument)
    if inst_data is None:
        log.warning("%s: not in H2_live_state.json -- skipping", instrument)
        return False

    sast_ts = live_state.get("generated_at_sast", "")
    v = extract_pine_values(inst_data, sast_ts)

    print_status(instrument, v)

    if verbose:
        print_paste_values(instrument, v)

    return True


# ───────────────────────────────────────────────────────────────────────────
# 6.  Main loop — all tier1 instruments
# ───────────────────────────────────────────────────────────────────────────

def run_once(
    instruments: list,
    verbose: bool = False,
) -> int:
    """
    One pass across all instruments.
    Returns count of successfully updated instruments.
    """
    live_state = load_live_state()
    if live_state is None:
        return 0

    gen_at = live_state.get("generated_at_sast", "?")[:19]
    print(f"\nTV bridge -- {len(instruments)} instrument(s) -- data as of {gen_at} SAST")
    print(
        f"Webhook    : "
        f"{'configured  ' + WEBHOOK_URL[:55] if WEBHOOK_URL else 'NOT SET in config'}"
    )
    print("-" * 80)

    ok = 0
    for inst in instruments:
        try:
            if update_instrument(inst, live_state, verbose=verbose):
                ok += 1
        except Exception as e:
            log.error("%s: unexpected error -- %s -- continuing to next instrument", inst, e)

    print("-" * 80)
    print(f"TV bridge -- pass complete  ({ok}/{len(instruments)} updated)\n")
    return ok


def run_loop(
    instruments: list,
    verbose: bool = False,
    interval: int = LOOP_INTERVAL,
):
    """
    Continuous 5-minute loop per spec:
      while True:
          for instrument in tier1_instruments:
              update_instrument(instrument)
          sleep(300)
    """
    log.info(
        "TV bridge loop started -- %d instruments -- interval %ds -- Ctrl+C to stop",
        len(instruments),
        interval,
    )
    while True:
        run_once(instruments, verbose=verbose)
        log.info("TV bridge -- sleeping %ds ...", interval)
        time.sleep(interval)


# ───────────────────────────────────────────────────────────────────────────
# 7.  CLI
# ───────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="H2 TradingView Bridge -- maps H2_live_state.json to Pine inputs"
    )
    parser.add_argument(
        "--instrument", "-i",
        default=None,
        help=(
            f"Single instrument to process (default: all tier1 from config = "
            f"{TIER1_INSTRUMENTS})"
        ),
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help=f"Run continuously every {LOOP_INTERVAL}s",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Print full copy-paste value block per instrument",
    )
    args = parser.parse_args()

    # Instrument list — always from config, never hardcoded
    if args.instrument:
        instruments = [args.instrument.upper()]
    else:
        instruments = TIER1_INSTRUMENTS   # list from config.yaml instruments.tier1

    if args.loop:
        run_loop(instruments, verbose=args.verbose)
    else:
        run_once(instruments, verbose=args.verbose)
