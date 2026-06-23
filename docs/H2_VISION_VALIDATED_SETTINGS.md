# H2 Vision System -- Validated Settings

Generated from 15M backtest on real IC Markets / yfinance data.

## JP225

**Bars:** 32 setups validated on 15M data

### Entry Parameters

| Parameter | Value |
|---|---|
| BOS pivot length | 14 bars (210 min) |
| sigma-2 lookback | 30 bars (450 min) |
| Fib box upper | 62% |
| Fib box lower | 90.0% |
| Min TF alignment | 2/4 |

### Performance

| Metric | Value |
|---|---|
| Best exit | ev_dest |
| EV per trade | +0.8173R |
| Win rate | 15.6% |
| Stop rate | 96.9% |

## USTEC

**Bars:** 55 setups validated on 15M data

### Entry Parameters

| Parameter | Value |
|---|---|
| BOS pivot length | 14 bars (210 min) |
| sigma-2 lookback | 30 bars (450 min) |
| Fib box upper | 50% |
| Fib box lower | 78.6% |
| Min TF alignment | 2/4 |

### Performance

| Metric | Value |
|---|---|
| Best exit | ev_dest |
| EV per trade | +1.9508R |
| Win rate | 30.9% |
| Stop rate | 94.5% |

## New Structure Level as Target

If new structure level is within 1.5x ATR of PWH/PWL/PDH/PDL:
- Use new structure level as Target Priority 1
- Size up to 1.5x normal
- Trail stop after TP1 (destination cluster)

## Oscillation Rules

Price visits VWAP destinations in sequence: 15M -> 1H -> 4H -> Daily.
Each level: collect EQH/EQL -> Fib pullback -> next leg.
2nd/3rd visit to same level = valid entry, tighter stop.
