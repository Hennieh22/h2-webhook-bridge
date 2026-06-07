"""
H2 Quant v1 — TradingView Bridge (Phase 8)
Reads H2_live_state.json every 5 minutes and:
  1. Prints formatted Pine Script input values (paste into TV indicator settings)
  2. Generates an updated .pine file with hardcoded current values
  3. Optionally POSTs to a simple local web server that TradingView
     can poll via request.security() (advanced — requires Pro subscription)

Usage:
  python pine/tv_bridge.py --instrument JP225
  python pine/tv_bridge.py --instrument JP225 --loop
  python pine/tv_bridge.py --instrument JP225 --generate-pine
"""

import io
import json
import sys
import time
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
import argparse
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUTPUTS_DIR = ROOT / "outputs"
PINE_DIR    = ROOT / "pine"
LIVE_STATE  = OUTPUTS_DIR / "H2_live_state.json"
SAST_OFFSET = timedelta(hours=2)


# ---------------------------------------------------------------------------
# Data loader
# ---------------------------------------------------------------------------

def load_live_state() -> dict:
    if not LIVE_STATE.exists():
        print(f"ERROR: {LIVE_STATE} not found. Run the monitor first.")
        sys.exit(1)
    with open(LIVE_STATE, encoding="utf-8") as f:
        return json.load(f)


def get_instrument_data(live_state: dict, instrument: str) -> dict:
    instr = live_state.get("instruments", {}).get(instrument)
    if not instr:
        available = list(live_state.get("instruments", {}).keys())
        print(f"ERROR: {instrument} not in live state. Available: {available}")
        sys.exit(1)
    return instr


# ---------------------------------------------------------------------------
# Value extractor — pulls all Pine input values from one instrument's data
# ---------------------------------------------------------------------------

def extract_pine_values(data: dict, instrument: str) -> dict:
    now_utc  = datetime.now(timezone.utc)
    now_sast = now_utc + SAST_OFFSET

    state_id    = data.get("current_state", "UNKNOWN")
    description = data.get("state_description", "No data")[:120]  # TV input length limit
    session     = data.get("session", "OFFHOURS")
    sast_time   = now_sast.strftime("%H:%M")

    next_states  = data.get("next_states", [])
    ns1 = next_states[0] if len(next_states) > 0 else {}
    ns2 = next_states[1] if len(next_states) > 1 else {}
    ns3 = next_states[2] if len(next_states) > 2 else {}

    gates = data.get("gates", {})
    g_gap  = gates.get("markov_gap",         {}).get("value", 0.0)
    g_pers = gates.get("markov_persistence",  {}).get("value", 0.0)
    g_vol  = gates.get("volatility_cap",      {}).get("value", 1.0)
    g_hurst= gates.get("hurst",               {}).get("value", 0.5)
    g_sess = gates.get("session",             {}).get("pass",  False)

    conv    = data.get("conviction",         "SKIP")
    pillars = data.get("pillars_confirmed",  0)
    wr      = data.get("historical_wr",      0.0)
    ev      = data.get("historical_ev",      0.0)
    samples = data.get("sample_count",       0)
    dwell_b = data.get("dwell_time_avg_bars", 0.0) or 0.0

    # Estimate bars in current state from dwell time (Phase 7 doesn't track this yet)
    dwell_in  = 1   # placeholder — Phase 7 monitor will track this

    return {
        "state":         state_id,
        "description":   description,
        "session":       session,
        "sast_time":     sast_time,
        "dwell_bars":    dwell_in,
        "dwell_avg":     round(float(dwell_b), 1),
        "next1":         ns1.get("state",       ""),
        "prob1":         round(float(ns1.get("probability", 0.0)), 4),
        "ev1":           round(float(ns1.get("ev",          0.0)), 3),
        "next2":         ns2.get("state",       ""),
        "prob2":         round(float(ns2.get("probability", 0.0)), 4),
        "ev2":           round(float(ns2.get("ev",          0.0)), 3),
        "next3":         ns3.get("state",       ""),
        "prob3":         round(float(ns3.get("probability", 0.0)), 4),
        "ev3":           round(float(ns3.get("ev",          0.0)), 3),
        "g_markov_gap":  round(float(g_gap),  4),
        "g_persistence": round(float(g_pers), 4),
        "g_vol_cap":     round(float(g_vol),  4),
        "g_hurst":       round(float(g_hurst),4),
        "g_session_ok":  bool(g_sess),
        "conviction":    conv,
        "pillars":       pillars,
        "hist_wr":       round(float(wr),      4),
        "hist_ev":       round(float(ev),      4),
        "samples":       samples,
    }


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------

def print_paste_values(v: dict, instrument: str):
    """Print values to paste into TradingView indicator settings."""
    sep = "=" * 72
    print(f"\n{sep}")
    print(f"  H2 STATE OVERLAY — INPUT VALUES FOR: {instrument}")
    print(f"  Copy-paste each value into the corresponding Pine input field")
    print(f"{sep}")
    print(f"\n  ── Current State ─────────────────────────────────────────")
    print(f"  Current State ID          : {v['state']}")
    print(f"  Plain English Description : {v['description']}")
    print(f"  Session                   : {v['session']}")
    print(f"  SAST Time                 : {v['sast_time']}")
    print(f"  Bars in State             : {v['dwell_bars']}")
    print(f"  Avg Dwell Time            : {v['dwell_avg']}")
    print(f"\n  ── Next States ───────────────────────────────────────────")
    print(f"  Next State 1  : {v['next1']}")
    print(f"  Probability 1 : {v['prob1']}   ({v['prob1']*100:.1f}%)")
    print(f"  EV 1          : {v['ev1']}")
    print(f"  Next State 2  : {v['next2']}")
    print(f"  Probability 2 : {v['prob2']}   ({v['prob2']*100:.1f}%)")
    print(f"  EV 2          : {v['ev2']}")
    print(f"  Next State 3  : {v['next3']}")
    print(f"  Probability 3 : {v['prob3']}   ({v['prob3']*100:.1f}%)")
    print(f"  EV 3          : {v['ev3']}")
    print(f"\n  ── Gate Values ───────────────────────────────────────────")
    print(f"  Gate 1 Markov Gap     : {v['g_markov_gap']}  {'PASS' if v['g_markov_gap'] >= 0.61 else 'FAIL'}")
    print(f"  Gate 2 Persistence    : {v['g_persistence']}  {'PASS' if v['g_persistence'] >= 0.82 else 'FAIL'}")
    print(f"  Gate 3 Vol Cap        : {v['g_vol_cap']}  {'PASS' if v['g_vol_cap'] <= 1.25 else 'FAIL'}")
    print(f"  Gate 4 Hurst          : {v['g_hurst']}  {'PASS' if v['g_hurst'] >= 0.50 else 'FAIL'}")
    print(f"  Gate 5 Session Pass   : {v['g_session_ok']}")
    print(f"\n  ── Conviction ────────────────────────────────────────────")
    print(f"  Conviction        : {v['conviction']}")
    print(f"  Pillars           : {v['pillars']}")
    print(f"  Historical WR     : {v['hist_wr']}   ({v['hist_wr']*100:.1f}%)")
    print(f"  Historical EV     : {v['hist_ev']}")
    print(f"  Sample Count      : {v['samples']}")
    print(f"{sep}\n")


def generate_pine_file(v: dict, instrument: str) -> Path:
    """
    Generate a ready-to-paste Pine Script file with hardcoded current values.
    Write to pine/H2_state_overlay_{instrument}_LIVE.pine
    """
    template_path = PINE_DIR / "H2_state_overlay_TEST.pine"
    if not template_path.exists():
        print("ERROR: Template file not found.")
        return None

    with open(template_path, encoding="utf-8") as f:
        content = f.read()

    # Replace the test indicator name
    content = content.replace(
        'indicator("H2 State Overlay [TEST]"',
        f'indicator("H2 State Overlay [{instrument}]"'
    )
    content = content.replace(
        'shorttitle="H2-TEST"',
        f'shorttitle="H2-{instrument}"'
    )

    # Replace hardcoded test data block
    replacements = {
        '"BULL_STRONG_HIGH_ABOVE_NY_BOS_OB"': f'"{v["state"]}"',
        '"JP225: bullish with strong momentum (high vol, BOS printed, RSI overbought, NY). Top transition (68%): pullback then continuation. Wait for fractal HL on retrace with RVOL declining."':
            f'"{v["description"]}"',
        '"NY"':    f'"{v["session"]}"',
        '"17:45"': f'"{v["sast_time"]}"',
        'i_dwell_bars  = 4':     f'i_dwell_bars  = {v["dwell_bars"]}',
        'i_dwell_avg   = 6.2':   f'i_dwell_avg   = {v["dwell_avg"]}',
        '"BULL_WEAK_NORMAL_ABOVE_NY_PULLBACK_NEUTRAL"': f'"{v["next1"]}"',
        'i_prob1       = 0.68': f'i_prob1       = {v["prob1"]}',
        'i_ev1         = 1.80': f'i_ev1         = {v["ev1"]}',
        '"BULL_STRONG_HIGH_ABOVE_NY_BOS_OB"': f'"{v["next2"]}"',  # next2 reuses same pattern
        'i_prob2       = 0.21': f'i_prob2       = {v["prob2"]}',
        'i_ev2         = 2.10': f'i_ev2         = {v["ev2"]}',
        '"BEAR_WEAK_HIGH_ABOVE_NY_CHOCH_OS"': f'"{v["next3"]}"',
        'i_prob3       = 0.11': f'i_prob3       = {v["prob3"]}',
        'i_ev3         = -0.40': f'i_ev3         = {v["ev3"]}',
        'i_g_markov_gap  = 0.68': f'i_g_markov_gap  = {v["g_markov_gap"]}',
        'i_g_persistence = 0.87': f'i_g_persistence = {v["g_persistence"]}',
        'i_g_vol_cap     = 1.05': f'i_g_vol_cap     = {v["g_vol_cap"]}',
        'i_g_hurst       = 0.61': f'i_g_hurst       = {v["g_hurst"]}',
        'i_g_session_ok  = true': f'i_g_session_ok  = {"true" if v["g_session_ok"] else "false"}',
        '"A+"':                f'"{v["conviction"]}"',
        'i_pillars     = 4':   f'i_pillars     = {v["pillars"]}',
        'i_hist_wr     = 0.71': f'i_hist_wr     = {v["hist_wr"]}',
        'i_hist_ev     = 1.80': f'i_hist_ev     = {v["hist_ev"]}',
        'i_samples     = 847': f'i_samples     = {v["samples"]}',
    }

    for old, new in replacements.items():
        content = content.replace(old, new, 1)  # replace first occurrence only

    out_path = PINE_DIR / f"H2_state_overlay_{instrument}_LIVE.pine"
    out_path.write_text(content, encoding="utf-8")
    print(f"Generated: {out_path}")
    return out_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_once(instrument: str, gen_pine: bool = False):
    live  = load_live_state()
    data  = get_instrument_data(live, instrument)
    v     = extract_pine_values(data, instrument)

    print(f"\nLast update: {live.get('generated_at_sast','?')[:19]} SAST")
    print(f"Instrument : {instrument}")
    print(f"State      : {v['state']}")
    print(f"Session    : {v['session']}")
    print(f"Conviction : {v['conviction']}  |  Pillars: {v['pillars']}/4")

    print_paste_values(v, instrument)

    if gen_pine:
        out = generate_pine_file(v, instrument)
        if out:
            print(f"Paste the contents of {out.name} into TradingView Pine Editor.")

    return v


def run_loop(instrument: str, interval: int = 300):
    print(f"TV Bridge running — refreshing every {interval}s. Press Ctrl+C to stop.")
    while True:
        try:
            run_once(instrument, gen_pine=True)
        except Exception as e:
            print(f"Error: {e}")
        print(f"Next refresh in {interval}s...")
        time.sleep(interval)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="H2 TradingView Bridge")
    parser.add_argument("--instrument", "-i", default="JP225",
                        help="Instrument to display (default: JP225)")
    parser.add_argument("--loop",         action="store_true",
                        help="Run continuously every 5 minutes")
    parser.add_argument("--generate-pine", action="store_true",
                        help="Generate a ready-to-paste .pine file")
    args = parser.parse_args()

    if args.loop:
        run_loop(args.instrument)
    else:
        run_once(args.instrument, gen_pine=args.generate_pine)
