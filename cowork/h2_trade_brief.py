#!/usr/bin/env python3
"""
H2 Trade Brief — Main Orchestrator
Usage: python cowork/h2_trade_brief.py
Or triggered by Cowork when you type "trade"
"""

import os
import sys
import json
import anthropic
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from h2_data_collector import fetch_all
from h2_destination    import build_destination_ladder
from h2_macro_engine   import compute_session_state, compute_instrument_permission

# ── Configuration ─────────────────────────────────────────────────────────────
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
OUTPUT_DIR    = Path("outputs/briefs")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

INSTRUMENTS = [
    "JP225","DE40","SPX","USTEC","US30","UK100","CAC40",
    "AUS200","HSI","NI225","XAUUSD","XAGUSD","USDJPY","EURUSD","GBPUSD"
]

HIT_RATES = {
    "JP225":  {"d1":0.601,"d2":0.402,"d3":0.334},
    "USTEC":  {"d1":0.374,"d2":0.345,"d3":0.030},
    "DEFAULT":{"d1":0.450,"d2":0.380,"d3":0.150},
}

def build_brief_payload(data: dict) -> str:
    """Convert collected data into structured JSON for Claude API"""
    now    = datetime.now(timezone.utc)
    macro  = data.get("macro", {})
    fx     = data.get("fx_quotes", {})
    comms  = data.get("commodities", {})

    session_state = compute_session_state(macro, fx)

    instruments_data = {}
    for instr in INSTRUMENTS:
        perm = compute_instrument_permission(instr, fx, macro, comms)
        rates = HIT_RATES.get(instr, HIT_RATES["DEFAULT"])
        instruments_data[instr] = {
            "macro_permission": perm["permission"],
            "macro_reason":     perm["reason"],
            "hit_rate_d1":      f"{rates['d1']*100:.0f}%",
            "note":             "Destination ladder requires live OHLC — use TradingView Panel 4 values",
        }

    payload = {
        "brief_time":    now.isoformat(),
        "session":       data.get("session", "UNKNOWN"),
        "session_state": session_state,
        "macro": {
            "US10Y":         macro.get("US10Y"),
            "US2Y":          macro.get("US2Y"),
            "yield_curve":   macro.get("yield_curve"),
            "yield_spread":  macro.get("yield_spread"),
            "DXY_direction": macro.get("DXY_proxy_direction"),
            "DXY_move_pct":  macro.get("DXY_move_pct"),
        },
        "fx_quotes":    fx,
        "commodities":  comms,
        "calendar": data.get("calendar", [])[:20],
        "news_status": {
            k: v.get("status","CLEAR")
            for k, v in data.get("news_status", {}).get("instruments", {}).items()
        },
        "instruments":   instruments_data,
        "live_state":    data.get("live_state", {}),
    }

    return json.dumps(payload, indent=2)

def call_claude_api(brief_json: str) -> str:
    """Send brief to Claude API and get the full intelligence report"""
    if not ANTHROPIC_KEY:
        return "[ERROR] ANTHROPIC_API_KEY not set in environment"

    system_path = Path(__file__).parent / "H2_BRIEF_SYSTEM.md"
    system_prompt = system_path.read_text() if system_path.exists() else \
        "You are the H2 Trading Intelligence Engine. Produce a comprehensive trading brief."

    print("[H2] Calling Claude API for intelligence synthesis...")

    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=8000,
        system=system_prompt,
        messages=[{
            "role": "user",
            "content": f"""Generate the complete H2 Trading Brief for this session.

QUANTITATIVE DATA (computed locally):
{brief_json}

INSTRUCTIONS:
1. Use the web_search tool to research the latest geopolitical news
   affecting financial markets. Search for:
   - "geopolitical risk markets today {datetime.now(timezone.utc).strftime('%B %Y')}"
   - "central bank news today"
   - "market moving news today"
   Then assess impact per instrument.

2. Analyse the economic calendar events provided.

3. Produce the complete H2 Trading Brief in the exact format
   specified in your system instructions.

4. Be specific with entry levels, stops, and targets.
   Use the validated hit rates and EV from the system instructions.

5. End with Panel Updates section showing exactly what to
   paste into Panel 2 (H2_News_v2) and Panel 3 (H2_Verdict).

IMPORTANT: The most critical sections are the RANKED OPPORTUNITIES table,
TOP 5 SETUPS with entry/stop/targets, and PANEL UPDATES. If you must
abbreviate anything, abbreviate the geopolitical narrative — never truncate
the trading opportunity sections."""
        }],
        tools=[{
            "type": "web_search_20250305",
            "name": "web_search"
        }]
    )

    report = ""
    for block in message.content:
        if hasattr(block, 'text'):
            report += block.text

    return report

def save_and_display(report: str, brief_json: str):
    """Save report to file and display in terminal"""
    now       = datetime.now(timezone.utc)
    timestamp = now.strftime("%Y%m%d_%H%M")
    filename  = OUTPUT_DIR / f"H2_brief_{timestamp}.md"

    with open(filename, 'w', encoding='utf-8') as f:
        f.write(f"# H2 Trading Brief — {now.strftime('%Y-%m-%d %H:%M UTC')}\n\n")
        f.write(report)
        f.write(f"\n\n---\n*Generated by H2 Trade Brief System*\n")

    data_file = OUTPUT_DIR / f"H2_brief_data_{timestamp}.json"
    with open(data_file, 'w') as f:
        f.write(brief_json)

    geo_path = Path("outputs/H2_geo_state.json")
    geo_state = {
        "updated_at": now.isoformat(),
        "source":     str(filename),
        "note": "Update Panel 2 geo_risk inputs from PANEL UPDATES section above"
    }
    with open(geo_path, 'w') as f:
        json.dump(geo_state, f, indent=2)

    print("\n" + "="*62)
    print(report)
    print("="*62)
    print(f"\n[OK] Brief saved to: {filename}")
    print(f"[OK] Raw data saved to: {data_file}")
    print(f"[OK] Geo state updated: {geo_path}")

def main():
    print("="*62)
    print("H2 TRADE BRIEF SYSTEM")
    print(f"Started: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print("="*62)

    data = fetch_all()
    brief_json = build_brief_payload(data)
    report = call_claude_api(brief_json)
    save_and_display(report, brief_json)

if __name__ == "__main__":
    main()
