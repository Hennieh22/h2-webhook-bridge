import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
import json

with open("outputs/H2_live_state.json") as f:
    d = json.load(f)

# Pick instrument with full pillar detail (written by live monitor, not bootstrap)
def has_full_pillars(v):
    p = v.get("confirmation_pillars", {})
    return any(isinstance(pv, dict) and "confirmed" in pv for pv in p.values())

candidates = [(k, v) for k, v in d["instruments"].items() if has_full_pillars(v)]
if not candidates:
    candidates = list(d["instruments"].items())
best = max(candidates, key=lambda x: (x[1].get("pillars_confirmed", 0), x[1].get("historical_ev", 0)))
k, v = best

print(f"\n{'='*70}")
print(f"  DETAILED GATE + PILLAR CHECK: {k}")
print(f"{'='*70}")
print(f"  State    : {v['current_state']}")
print(f"  Session  : {v['session']}")
print(f"  Pillars  : {v['pillars_confirmed']}/4   Conviction: {v['conviction']}")
print(f"  Direction: {v.get('direction','?')}")
print(f"  EV       : {v.get('historical_ev',0):+.3f}R  |  WR: {v.get('historical_wr',0):.0%}  |  Samples: {v.get('sample_count',0)}")
print()

print("  GATES:")
for gk, gv in v["gates"].items():
    status = "PASS" if gv["pass"] else "FAIL"
    ref = gv.get("threshold", gv.get("required", "ANY"))
    print(f"    {gk:<28} value={str(gv['value']):<12}  ref={str(ref):<14}  [{status}]")

print()
print("  CONFIRMATION PILLARS:")
pillars = v.get("confirmation_pillars", {})
for pk, pv in pillars.items():
    if isinstance(pv, dict) and "confirmed" in pv:
        status = "YES" if pv["confirmed"] else "NO "
        print(f"    {pk:<12} [{status}]  {pv['signal']}")
    else:
        print(f"    {pk:<12}  {pv}")

print()
print("  TOP 3 NEXT STATES:")
for ns in v.get("next_states", [])[:3]:
    print(f"    {ns['state']:<55} ({ns['probability']:.0%})  ev={ns['ev']:+.3f}R")

print()
print("  DESCRIPTION:")
print(f"    {v.get('state_description','')}")

# WhatsApp message preview
from live.monitor import build_whatsapp_message, build_webhook_payload
signal = build_webhook_payload(
    k, v.get("direction","BUY"), v["current_state"],
    v.get("next_states",[{}])[0].get("probability",0) if v.get("next_states") else 0,
    v.get("historical_ev",0), v.get("session","?"),
    v.get("conviction","SKIP"), v.get("pillars_confirmed",0),
)
msg = build_whatsapp_message(
    signal, k, v.get("state_description",""),
    v.get("confirmation_pillars", {}),
)
print(f"\n{'='*70}")
print("  WHATSAPP MESSAGE PREVIEW (DRY RUN):")
print(f"{'='*70}")
print(msg)
print(f"{'='*70}")
