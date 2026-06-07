import json
from pathlib import Path

INSTRUMENTS = ['JP225','DE40','UK100','USTEC','US30','HK50',
               'EURUSD','GBPUSD','USDJPY','AUDUSD','USDCAD',
               'EURJPY','GBPJPY','XAUUSD','XAGUSD']
TIMEFRAMES = ['D','4H','1H','15m']

print()
print('='*97)
print('  PHASE 5 COMPLETE -- RESEARCH VALIDATOR BATCH SUMMARY')
print('='*97)
hdr = f"  {'INSTRUMENT':<10} {'TF':<5} {'STATES':>7} {'SHARPE':>8} {'MED DD':>8} {'RUIN%':>7} {'OOS OK':>7} {'STABLE>=0.7':>12}"
print(hdr)
print('  ' + '-'*95)

total_states = 0
for inst in INSTRUMENTS:
    for tf in TIMEFRAMES:
        p = Path(f'outputs/H2_state_stats_{inst}_{tf}.json')
        if not p.exists():
            print(f'  {inst:<10} {tf:<5} MISSING')
            continue
        with open(p) as f:
            d = json.load(f)
        states  = d.get('states', {})
        n       = len(states)
        mc      = d.get('monte_carlo', {})
        sharpe  = mc.get('median_sharpe', 0)
        med_dd  = mc.get('p50_max_drawdown', 0)
        ruin    = mc.get('probability_of_ruin', 0) * 100
        oos_ok  = sum(1 for s in states.values() if not s.get('oos_flagged', False))
        stable  = sum(1 for s in states.values() if s.get('stability_score', 0) >= 0.7)
        total_states += n
        row = (f"  {inst:<10} {tf:<5} {n:>7} {sharpe:>+8.2f} {med_dd:>7.1f}R"
               f" {ruin:>6.1f}% {oos_ok:>7} {stable:>12}")
        print(row)

print('='*97)
print(f"  Total states with stats: {total_states}")
print('='*97)
