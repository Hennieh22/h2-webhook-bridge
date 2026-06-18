#!/usr/bin/env python3
"""
H2 Monitor — Background destination flip detector
Runs continuously, fires WhatsApp when D1 destination is crossed
and next destination becomes active.

Usage: python cowork/h2_monitor.py
Runs every SCAN_INTERVAL seconds (default 900 = 15 minutes)
"""

import time
import os
from datetime import datetime, timezone
from h2_scan import run_scan, send_whatsapp, get_current_session

SCAN_INTERVAL = int(os.environ.get("H2_SCAN_INTERVAL", "900"))  # 15 min default

def main():
    print(f"[H2 MONITOR] Starting -- scanning every {SCAN_INTERVAL}s")
    print(f"[H2 MONITOR] Press Ctrl+C to stop")

    scan_count = 0
    while True:
        try:
            session = get_current_session()
            if session in ["LONDON", "NY"]:
                scan_count += 1
                now = datetime.now(timezone.utc).strftime("%H:%M UTC")
                print(f"\n[{now}] Scan #{scan_count} -- {session} session")
                results, session = run_scan(verbose=False)
                active = [r for r in results
                         if r["in_session"] and r["news"] == "CLEAR"]
                print(f"  Active instruments: {len(active)}")
                for r in active[:5]:
                    print(f"  {r['instr']}: D1={r['d1_dest']} "
                          f"({r['d1_dist_r']}R) EV={r['ev_d1d2']}R")
            else:
                print(f"[{datetime.now(timezone.utc).strftime('%H:%M')}] "
                      f"Asia session -- reduced scan frequency")

            time.sleep(SCAN_INTERVAL)

        except KeyboardInterrupt:
            print("\n[H2 MONITOR] Stopped by user")
            break
        except Exception as e:
            print(f"[H2 MONITOR] Error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    main()
