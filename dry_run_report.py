"""
H2 Phase 7 — Full Tier 1 dry-run report.
Prints per-instrument: state, gates, conviction, WA message, webhook payload.
Does NOT fire anything.
"""
import sys, io, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import yaml
from pathlib import Path

ROOT = Path(__file__).resolve()
with open(ROOT.parent / "config.yaml") as f:
    CFG = yaml.safe_load(f)

# Load existing live state (written by last monitor run)
live_path = ROOT.parent / "outputs" / "H2_live_state.json"
with open(live_path) as f:
    live = json.load(f)

# Import helpers
sys.path.insert(0, str(ROOT.parent))
from live.monitor import (
    build_webhook_payload,
    build_whatsapp_message,
    state_to_english,
)

TIER1 = CFG["instruments"]["tier1"]
SEP   = "=" * 72

print(f"\n{SEP}")
print("  H2 TIER 1 DRY-RUN REPORT")
print(f"  Session: {live.get('session','?')}  |  Generated: {live.get('generated_at_sast','?')[:19]} SAST")
print(SEP)

for inst in TIER1:
    info = live.get("instruments", {}).get(inst)
    if not info:
        print(f"\n  {inst}: no data in live state")
        continue

    state   = info.get("current_state", "?")
    sess    = info.get("session", "?")
    gates   = info.get("gates", {})
    pillars = info.get("pillars_confirmed", 0)
    conv    = info.get("conviction", "SKIP")
    ev      = info.get("historical_ev", 0.0)
    wr      = info.get("historical_wr", 0.0)
    samples = info.get("sample_count", 0)
    direction = info.get("direction", "BUY")
    next_st = info.get("next_states", [])
    top_prob = next_st[0]["probability"] if next_st else 0.0
    all_pass = info.get("all_gates_pass", False)

    print(f"\n{'─'*72}")
    print(f"  {inst}  |  {sess}  |  Conv: {conv}  |  Pillars: {pillars}/4")
    print(f"  State: {state}")
    print(f"  EV: {ev:+.3f}R  |  WR: {wr:.0%}  |  Samples: {samples}")
    print()

    # Gates
    print("  GATES:")
    for gk, gv in gates.items():
        status = "PASS" if gv["pass"] else "FAIL"
        val    = str(gv.get("value", "?"))
        ref    = str(gv.get("threshold", gv.get("required", "ANY")))
        mark   = "[PASS]" if gv["pass"] else "[FAIL]"
        print(f"    {gk:<28} {val:<12} vs {ref:<15} {mark}")

    # Pillars
    pillar_detail = info.get("confirmation_pillars", {})
    if any(isinstance(v, dict) for v in pillar_detail.values()):
        print()
        print("  PILLARS:")
        for pk, pv in pillar_detail.items():
            if isinstance(pv, dict):
                ok = "[YES]" if pv.get("confirmed") else "[NO ]"
                print(f"    {pk:<12} {ok}  {pv.get('signal','')}")

    # Next states
    if next_st:
        print()
        print("  NEXT STATES (top 3):")
        for ns in next_st[:3]:
            print(f"    {ns['state']:<55}  {ns['probability']:.0%}  ev={ns['ev']:+.3f}R")

    # Webhook payload (what WOULD fire)
    print()
    print("  WEBHOOK PAYLOAD (dry run — NOT sent):")
    payload = build_webhook_payload(
        inst, direction, state, top_prob, ev, sess, conv, pillars
    )
    for k, v in payload.items():
        print(f"    {k:<22} {v}")

    # WhatsApp message (what WOULD be sent)
    desc = info.get("state_description", state_to_english(state, inst, next_st))
    msg  = build_whatsapp_message(payload, inst, desc, pillar_detail)
    print()
    print("  WHATSAPP MESSAGE (dry run — NOT sent):")
    for line in msg.split("\n"):
        print(f"    {line}")

print(f"\n{SEP}")
print("  DRY RUN COMPLETE — no webhooks or WhatsApp were sent")
print(f"{SEP}")
