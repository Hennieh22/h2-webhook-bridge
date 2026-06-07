# CLAUDE.md — H2 Quant System v1
# H2 Systematic Trading · IC Markets MT5 · George, South Africa (SAST / UTC+2)

## Project Overview
This is a five-part quantitative trading research and execution system.
It is NOT an indicator system. It is a market state machine that:
1. Classifies every bar into a composite market state
2. Builds Markov transition matrices to forecast next states
3. Validates which states have statistical edge
4. Confirms state activation via 4-pillar evidence system
5. Delivers a live session brief via Claude Cowork on demand

---

## Project Structure

H2_Quant_v1/
├── CLAUDE.md
├── config.yaml
├── data/
│   ├── raw/              # MT5 CSV exports or API pulls
│   └── processed/        # Parquet files
├── features/
│   └── engineer.py       # All 22 market dimensions
├── states/
│   └── classifier.py     # Composite state ID builder
├── markov/
│   └── engine.py         # Transition matrix + forecasting
├── research/
│   └── validator.py      # Per-state stats, walk-forward, Monte Carlo
├── live/
│   └── monitor.py        # 5-minute loop, gate checker, webhook
├── briefing/
│   └── generator.py      # Daily/session brief + H2_live_state.json
├── outputs/
│   ├── H2_live_state.json
│   ├── H2_transition_matrix_{instrument}.json
│   ├── H2_state_stats.json
│   ├── H2_dwell_times.json
│   ├── H2_signal_log.csv
│   ├── H2_backtest_report.html
│   └── H2_daily_briefing.md
└── pine/
    └── H2_state_overlay.pine

---

## Instruments
Primary (indices):   JP225, DE40, UK100, USTEC, US30, HK50
Extended (50 total): All IC Markets indices + major forex pairs
                     (EURUSD, GBPUSD, USDJPY, AUDUSD, USDCAD,
                      EURJPY, GBPJPY, XAUUSD, XAGUSD + others)

## Timeframes
Research:  Daily, 4H, 1H, 15m
Live:      5m (classification + webhook trigger)
Reference: 1H + 4H (MTF confirmation)

## Data Sources (priority order)
1. MetaTrader5 Python API — primary (pip install MetaTrader5)
   MT5 must be running on Windows. Pulls exact IC Markets data.
2. yfinance — fallback for instruments not on IC Markets
3. Stooq via pandas-datareader — fallback for daily index data

---

## Part A — Research Engine

### Data format (MT5 CSV)
Columns: Date, Time, Open, High, Low, Close, Volume
Parse date+time as UTC. Add SAST column (UTC+2).
Save as Parquet: data/processed/H2_raw_{instrument}_{tf}.parquet

### Feature engineering — 22 market dimensions
Every computed feature outputs THREE fields:
  { value: float, confidence: float (0.0–1.0), reasoning: string }

Dimensions to compute per bar:
  1.  Price structure:    OHLC, typical price, log returns
  2.  Volume:            RVOL vs 20-bar avg, delta proxy, cumulative delta
  3.  Volatility:        ATR(14), BBWidth(20,2), Garman-Klass, GARCH(1,1)
  4.  Momentum:          RSI(14), MACD histogram, Williams %R(14), CCI
  5.  Liquidity:         Distance to swing H/L, FVG presence, imbalance flag
  6.  Time:              Session label, SAST hour, day-of-week, kill zone flag
  7.  Trend:             EMA21 vs EMA50 vs EMA200 alignment
  8.  Market structure:  HH/HL/LH/LL state, BOS flag, CHoCH flag, MSS flag
  9.  Order flow:        Delta proxy (close>open=buy), session delta cumulative
  10. Market profile:    VWAP deviation, session POC distance
  11. Volatility regime: ATR percentile rank over 50 bars (Low/Normal/High)
  12. Sentiment:         Williams VIX Fix (22,9) — fear proxy
  13. Correlation:       Rolling 20-bar correlation to DXY proxy
  14. Efficiency:        Kaufman Efficiency Ratio(10), Hurst exponent(50)
  15. Range:             Daily range vs 20-day avg, IB range, range position %
  16. Imbalance:         FVG above/below price, single print flag
  17. Fractality:        Fractal high/low (Williams, lookback=5), nested fractals
  18. Breadth:           Not applicable for single instrument — skip or use DXY
  19. Open interest:     If available via broker feed — else skip
  20. Accumulation/Dist: OBV slope, A/D line slope
  21. Risk:              ATR-based stop distance, R-multiple zones
  22. MTF context:       4H state label, 1H state label (for 5m/15m charts)

Output: features/H2_features_{instrument}_{tf}.parquet

### State classifier — composite state ID
Format: {TREND}_{MOMENTUM}_{VOLATILITY}_{LIQUIDITY}_{SESSION}_{STRUCTURE}_{RSI}
All caps. Underscore-separated.
Example: BULL_STRONG_HIGH_ABOVE_NY_BOS_OB

Dimension labels:
  TREND:      BULL / BEAR / NEUTRAL
  MOMENTUM:   STRONG / WEAK / NEUTRAL
  VOLATILITY: HIGH / NORMAL / LOW
  LIQUIDITY:  ABOVE / BELOW / INSIDE  (relative to nearest swing extreme)
  SESSION:    TOKYO / LONDON / NY / OVERLAP / OFFHOURS
  STRUCTURE:  BOS / CHOCH / MSS / RANGE / TREND_CONT / PULLBACK
  RSI:        OB / OS / NEUTRAL  (OB=>65, OS=<35)

Hierarchy (structure takes precedence):
  MSS > CHOCH > BOS > PULLBACK > RANGE > TREND_CONT

Output: states/H2_states_{instrument}_{tf}.parquet (adds 'state_id' column)

### Markov engine
- N×N transition count matrix indexed by composite state_id string
- Normalise rows to transition probabilities
- Session-conditioned sub-matrices: one each for TOKYO, LONDON, NY
- Chapman-Kolmogorov n-step forecast: 1, 3, 5, 10 bars ahead
- Stationary distribution: long-run probability of each state
- Dwell time: average bars in each state before transition
- Bayesian posterior: update transition probabilities bar-by-bar
- MINIMUM 30 samples per state before publishing any statistics

Output:
  outputs/H2_transition_matrix_{instrument}.json
  outputs/H2_dwell_times.json

### Research validator
Per-state statistics (minimum 30 samples required):
  - Win rate (WR)
  - Expectancy value (EV)
  - Profit factor (PF)
  - Average R-multiple
  - Max adverse excursion (MAE)
  - Max favorable excursion (MFE)
  - Sample count + 95% confidence interval

Breakdowns:
  - Per session (Tokyo / London / NY)
  - Per instrument
  - Per transition pair (state A → state B performance)

Validation:
  - Walk-forward only — NO lookahead bias
  - Train: 80% of history, OOS: most recent 20%
  - Monte Carlo: 10,000 paths from transition matrix
  - Report: Sharpe ratio, max drawdown, regime classification accuracy

Output:
  outputs/H2_state_stats.json
  outputs/H2_backtest_report.html

---

## Part B — Live State Monitor

File: live/monitor.py
Runs every 5 minutes continuously.

For each instrument (all 50):
  1. Pull latest bar from MT5 Python API
  2. Compute all 22 features (same logic as Part A)
  3. Classify current state → composite state_id
  4. Query transition matrix → P(next state), ranked top 3
  5. Check all 5 gates (see Gates section below)
  6. If all gates pass → format webhook JSON → fire to Railway bridge
  7. Update H2_live_state.json
  8. Append to H2_signal_log.csv

### H2_live_state.json structure
Must be self-contained and human-readable (used by both Part C and Part E).

{
  "generated_at_utc": "2025-01-15T13:45:00Z",
  "generated_at_sast": "2025-01-15T15:45:00+02:00",
  "session": "NY",
  "instruments": {
    "JP225": {
      "current_state": "BULL_STRONG_HIGH_ABOVE_NY_BOS_OB",
      "state_description": "Bull trend, strong momentum, high volatility, above liquidity, NY session, BOS printed, RSI overbought",
      "next_states": [
        {
          "state": "BULL_WEAK_NORMAL_ABOVE_NY_PULLBACK_NEUTRAL",
          "probability": 0.48,
          "ev": 1.8,
          "description": "Pullback into previous BOS level"
        },
        {
          "state": "BULL_STRONG_HIGH_ABOVE_NY_BOS_OB",
          "probability": 0.31,
          "ev": 2.1,
          "description": "Continuation — momentum holds"
        },
        {
          "state": "BEAR_WEAK_HIGH_ABOVE_NY_CHOCH_OS",
          "probability": 0.21,
          "ev": -0.4,
          "description": "Reversal risk — watch CHoCH"
        }
      ],
      "gates": {
        "markov_gap":         { "value": 0.48, "threshold": 0.11, "pass": true },
        "markov_persistence": { "value": 0.87, "threshold": 0.82, "pass": true },
        "volatility_cap":     { "value": 1.05, "threshold": 1.25, "pass": true },
        "hurst":              { "value": 0.61, "threshold": 0.50, "pass": true },
        "session":            { "value": "NY", "required": "NY",  "pass": true }
      },
      "all_gates_pass": true,
      "confirmation_pillars": {
        "volume":    { "signal": "RVOL 1.8x, delta positive",       "confirmed": true },
        "structure": { "signal": "BOS printed 3 bars ago",           "confirmed": true },
        "fractals":  { "signal": "Fractal HL holding at 34850",      "confirmed": true },
        "mtf":       { "signal": "4H BULL, 1H BULL — aligned",       "confirmed": true }
      },
      "pillars_confirmed": 4,
      "conviction": "A+",
      "historical_wr": 0.71,
      "historical_ev": 1.8,
      "sample_count": 847,
      "dwell_time_avg_bars": 6.2
    }
  }
}

### Gates — all 5 must pass to fire webhook
  1. Markov gap:         top probability >= 50% + 11pp (i.e. >= 61%)
  2. Markov persistence: self-transition probability >= 82%
  3. Volatility cap:     current ATR <= 1.25x Garman-Klass reference
  4. Hurst exponent:     Hurst >= 0.50 (trending regime only)
  5. Session timing:     NY session (13:00–20:00 UTC) for JP225
                         London session (07:00–16:00 UTC) for DE40, UK100
                         Any session for forex pairs

### Webhook format (Railway bridge → MetaAPI → MT5)
{
  "action":            "ENTER",
  "symbol":            "JP225",
  "direction":         "BUY",
  "size":              0.1,
  "state":             "BULL_STRONG_HIGH_ABOVE_NY_BOS_OB",
  "confidence":        0.87,
  "ev":                1.8,
  "session":           "NY",
  "conviction":        "A+",
  "pillars_confirmed": 4
}

### Signal log — H2_signal_log.csv
Columns:
  timestamp_utc, sast_time, instrument, direction, state_id,
  confidence, ev, session, pillars_confirmed, conviction,
  gate_markov_gap, gate_persistence, gate_vol, gate_hurst,
  gate_session, fired, outcome_r

---

## Part C — TradingView Overlay

File: pine/H2_state_overlay.pine
Pine Script v5. Display only — no signal generation.

Displays on chart:
  - Current state ID label (top-left table)
  - Top 3 next states with probability percentages
  - Gate status panel (5 gates as CHECK / WARN / FAIL)
  - Conviction rating (A+ / A / B / SKIP)
  - Pillar confirmation count (e.g. "3/4 pillars")

Data source: reads from webhook endpoint serving H2_live_state.json

---

## Part D — Confirmation Architecture (4-Pillar Entry System)

Converts Markov next-state probabilities into specific chart evidence.
When Markov says "next state = X with 68% probability",
Part D defines exactly what to look for to confirm X is activating.

### Pillar 1 — Volume
  EXPANSION activating:    RVOL >= 1.5x, delta positive, session open surge
  REACTION activating:     Delta flip sell→buy, CVD turning, WVF spike
  PULLBACK activating:     RVOL declining, delta neutral, volume dry-up
  DISTRIBUTION activating: CVD diverging from price, delta weakening
  LIQ_SWEEP activating:    RVOL spike >= 2x on wick candle, immediate delta flip
  COMPRESSION:             Volume < 0.7x 20-bar average

### Pillar 2 — Structure
  EXPANSION:    BOS printed on entry TF
  REACTION:     MSS + LTF CHoCH confirmed
  PULLBACK:     HL holding, no CHoCH, structure intact
  DISTRIBUTION: CHoCH printed on entry TF
  LIQ_SWEEP:    Wick through structure level + reclaim within 3 bars
  COMPRESSION:  No BOS/CHoCH for 15+ bars, IB respected

### Pillar 3 — Fractals
  EXPANSION:    Fractal HL forms + holds after pullback
  REACTION:     Fractal low prints at sweep point — key timing signal
  PULLBACK:     LTF fractal high forms at 50–62% retrace zone
  DISTRIBUTION: Equal highs forming = external liquidity target
  LIQ_SWEEP:    Fractal swept + price reclaims within 1–3 bars
  COMPRESSION:  Fractals contracting — range narrowing

### Pillar 4 — MTF Fluency
  Required alignment for entry:
    4H state + 1H state must agree on direction
    Entry TF (5m/15m) must confirm with structure event

  High conviction combinations:
    4H BULL + 1H PULLBACK + 5m BOS         = continuation entry
    4H BULL + 1H CHOCH + 5m MSS            = reversal entry (fade)
    4H RANGE + 1H LIQ_SWEEP + 5m REACTION  = precision reversal
    4H BEAR + 1H PULLBACK + 5m BOS_DOWN    = short continuation

### Confirmation scoring
  4/4 pillars = A+  — take the trade, full size
  3/4 pillars = A   — take the trade, reduced size
  2/4 pillars = B   — wait for one more confirmation
  1/4 pillars = SKIP — do not trade this setup

---

## Part E — Session Brief (Claude Cowork)

### Trigger command
User types: "H2 brief" or "H2 session report"

### What Cowork reads
  - outputs/H2_live_state.json   (current state for all 50 instruments)
  - outputs/H2_state_stats.json  (historical EV/WR per state)
  - outputs/H2_signal_log.csv    (what has fired today already)
  - Current UTC time             (derive session, time to kill zones)

### Report structure (to be fully designed in a separate session)

  Section 1: Session context header
    — Current session, SAST time, time to next kill zone
    — Overall market regime (risk-on / risk-off / mixed)

  Section 2: Top 5 opportunities RIGHT NOW
    — Instrument · State · Top probability · Pillars confirmed · Conviction
    — One-line plain English description per instrument
    Example: "JP225 is in a bull continuation state. Pullback to BOS
              level expected. 3/4 pillars confirmed. Wait for fractal HL."

  Section 3: Full ranked table — all 50 instruments
    — Sorted by: EV × confidence × pillars_confirmed
    — Traffic light: GREEN (A+/A) · AMBER (B) · RED (SKIP/AVOID)
    — Columns: Instrument · State · Top next state · P% · WR · EV · Pillars · Conviction

  Section 4: Avoid list
    — Instruments in COMPRESSION or DISTRIBUTION
    — States with sample count < 30
    — Probability spread too flat (top state < 40%)

  Section 5: Risk flags
    — News events in next 2 hours
    — Instruments where today's session is historically weak
    — States that have fired and failed already today

### Report design principle
  Must answer in under 60 seconds of reading:
  — Which 2–3 instruments deserve attention RIGHT NOW
  — What state they are in
  — What the top probability transition is
  — What specific evidence to look for before entering
  Everything else is supporting context.

---

## General Rules

### Code standards
  - Python 3.11
  - Libraries: pandas, numpy, scipy, scikit-learn, hmmlearn,
               pandas-ta, MetaTrader5, yfinance, plotly,
               statsmodels, arch, pyarrow, requests
  - All output files saved to outputs/ directory
  - All timestamps stored as UTC, displayed as SAST (UTC+2)
  - All configuration in config.yaml — no hardcoded values
  - Logging to logs/ directory with timestamps

### Statistical rules
  - Minimum 30 samples before any state statistic is trusted
  - Walk-forward validation only — absolutely no lookahead bias
  - OOS period = most recent 20% of data
  - Confidence intervals reported on all WR/EV figures
  - Monte Carlo: minimum 10,000 paths

### What this system does NOT do
  - Does not use indicator crossover signals
  - Does not apply fixed rules (e.g. RSI > 70 = sell)
  - Does not fire signals without all 5 gates passing
  - Does not trust any state with fewer than 30 samples
  - Does not run backtests with lookahead bias

---

## Build Order

Phase 1: data/        — MT5 loader + yfinance fallback
Phase 2: features/    — All 22 dimensions
Phase 3: states/      — Composite state classifier
Phase 4: markov/      — Transition matrix + forecasting
Phase 5: research/    — Validator + backtest report
Phase 6: briefing/    — H2_live_state.json generator
Phase 7: live/        — 5-minute monitor loop + webhook
Phase 8: pine/        — TradingView overlay script
Phase 9: cowork/      — Session brief command + report template

---
## End of CLAUDE.md
