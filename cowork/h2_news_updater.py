#!/usr/bin/env python3
"""
H2 News Updater
Reads outputs/h2_news_state.json (written by h2_trade_brief.py) and:
  1. Prints a formatted instruction for Cowork's computer-use to update
     the H2·NEWS indicator settings in TradingView.
  2. Saves that instruction to outputs/h2_news_update_instruction.txt

Called automatically by trade.bat and auto_trade.bat after the brief runs.
Cowork reads the instruction file and executes the TradingView update.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path


STATE_FILE       = Path("outputs/h2_news_state.json")
INSTRUCTION_FILE = Path("outputs/h2_news_update_instruction.txt")


def load_news_state() -> dict | None:
    if not STATE_FILE.exists():
        print("[NEWS UPDATER] No state file found — run trade brief first")
        print(f"               Expected: {STATE_FILE.resolve()}")
        return None
    try:
        with open(STATE_FILE, encoding="utf-8-sig") as f:
            state = json.load(f)
        age_s = (datetime.now(timezone.utc).timestamp() -
                 datetime.fromisoformat(state.get("extracted_at",
                     datetime.now(timezone.utc).isoformat())).timestamp())
        if age_s > 14400:
            print(f"[NEWS UPDATER] Warning: state file is {age_s/3600:.1f}h old")
        return state
    except Exception as e:
        print(f"[NEWS UPDATER] Failed to load state: {e}")
        return None


def format_instruction(state: dict) -> str:
    """
    Returns a clear, step-by-step instruction for Cowork's computer-use
    to update the H2·NEWS v2 indicator settings in TradingView.

    Cowork should execute these steps literally using screenshot + click tools.
    """
    mins = state.get("mins_away", 999)
    mins_display = "999 (none)" if mins >= 999 else str(mins)

    lines = [
        "=" * 68,
        "H2 NEWS PANEL UPDATE — COWORK COMPUTER-USE INSTRUCTION",
        f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "=" * 68,
        "",
        "OBJECTIVE: Update the H2·NEWS v2 indicator settings in TradingView.",
        "",
        "PRE-FLIGHT:",
        "  1. Take a screenshot to confirm TradingView is open.",
        "  2. If TradingView is not open: open Microsoft Edge, navigate to",
        "     https://www.tradingview.com and open the H2 Indices System chart.",
        "  3. Confirm the chart is loaded and the H2·NEWS v2 indicator is visible.",
        "",
        "STEPS:",
        "  1. Find 'H2·NEWS·v2' in the indicator label area at the top of the chart.",
        "  2. Hover over it — a small toolbar appears (eye / settings gear / ✕).",
        "  3. Click the SETTINGS GEAR icon (⚙).",
        "  4. The indicator settings dialog opens. Switch to MANUAL MODE:",
        "     - Uncheck 'AUTO MODE' if it is currently checked.",
        "  5. Find the group 'Manual Mode — paste from Railway'.",
        "     Set these fields EXACTLY (copy-paste each value):",
        "",
        f"     News Status       →  {state['news_status']}",
        f"     Next Event Title  →  {state.get('next_event', '') or '(leave blank)'}",
        f"     Minutes to Next   →  {mins_display}",
        f"     Impact            →  {state.get('event_impact', 'Low')}",
        f"     Breaking News     →  {state.get('breaking_text', '') or '(leave blank)'}",
        f"     Session Risk      →  {state.get('session_risk', 'LOW')}",
        f"     Last Updated UTC  →  {state.get('last_updated', '')}",
        "",
        "  6. Find the group 'Geo Risk Override'. Set:",
        f"     Geo Risk          →  {state.get('geo_risk', 'NONE')}",
        "",
        "  7. Click OK to apply and close the dialog.",
        "",
        "VERIFICATION:",
        "  - Take a screenshot of the chart.",
        "  - Confirm the H2·NEWS table in the chart shows:",
        f"      SESSION STATUS = {state['news_status']}",
        f"      GEO RISK       = {state.get('geo_risk', 'NONE')}",
        "  - Report: 'News panel updated successfully' or describe any issue.",
        "",
        "=" * 68,
        "SOURCE: outputs/h2_news_state.json",
        f"EXTRACTED AT: {state.get('extracted_at', 'unknown')}",
        "=" * 68,
    ]
    return "\n".join(lines)


def main():
    state = load_news_state()
    if state is None:
        sys.exit(1)

    instruction = format_instruction(state)

    # Print to terminal (visible in trade.bat output)
    print(instruction)

    # Save instruction file for Cowork to pick up
    INSTRUCTION_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(INSTRUCTION_FILE, "w", encoding="utf-8") as f:
        f.write(instruction)

    print(f"\n[NEWS UPDATER] Instruction saved → {INSTRUCTION_FILE.resolve()}")
    print("[NEWS UPDATER] Cowork: read outputs/h2_news_update_instruction.txt")
    print("[NEWS UPDATER] and execute the computer-use steps described there.")


if __name__ == "__main__":
    main()
