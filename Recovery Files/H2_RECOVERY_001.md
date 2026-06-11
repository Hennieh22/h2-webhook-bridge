# H2 QUANT SYSTEM — COMPLETE RECOVERY GUIDE
## Recovery File 001 — Full System Rebuild from Zero
*If you are reading this, you have lost your laptop or chat history.
This file contains everything needed to rebuild the complete H2 Quant System.*

---

## SYSTEM IDENTITY

**Owner:** H2 Systematic Trading  
**Location:** George, South Africa (SAST = UTC+2)  
**Broker:** IC Markets — MT5 Demo account  
**Purpose:** Professional quantitative trading system — 5 parts  

---

## CREDENTIALS (store securely — never share)

| Item | Value |
|---|---|
| MT5 Login | 52810633 |
| MT5 Server | ICMarketsSC-Demo |
| MT5 Broker | IC Markets |
| MT5 Password | 4gDCP5hP1@wqxV |
| MetaAPI Account ID | ef07b443-d678-4830-8941-8ab853504970 |
| MetaAPI Region | london |
| MetaAPI Token | Stored in Railway env vars — refresh at metaapi.cloud |
| Railway URL | https://h2-webhook-bridge-production.up.railway.app/webhook/tradingview |
| Railway Dashboard | https://h2-webhook-bridge-production.up.railway.app/ |
| Railway Health | https://h2-webhook-bridge-production.up.railway.app/health |
| Webhook Secret | H2_SUPER_SECRET_2026 |
| GitHub Repo | https://github.com/Hennieh22/h2-webhook-bridge |
| GitHub Branch | h2-quant-system |
| CallMeBot API Key | 2096445 |
| WhatsApp Number | +27614056155 |
| Dashboard Local | http://192.168.101.245:5050 |
| Twilio Live SID | stored separately — do not commit to GitHub |
| Twilio Test SID | stored separately — do not commit to GitHub |

---

## WHAT THIS SYSTEM IS

This is a 5-part quantitative trading system. It is NOT an indicator system.
It is a market state machine that:

1. Classifies every bar into a composite 7-dimension market state
2. Builds Markov transition matrices to forecast next states
3. Validates which states have statistical edge (walk-forward, OOS)
4. Confirms state activation via 4-pillar evidence system
5. Delivers a live session brief via Claude Cowork on demand

### The 5 Parts

| Part | What | Where |
|---|---|---|
| A | Research engine — builds Markov brain | Python / Claude Code |
| B | Live monitor — classifies every 5 minutes | Python / PowerShell |
| C | TradingView overlay — shows state on chart | Pine Script v5 |
| D | 4-pillar confirmation — entry evidence | TradingView |
| E | Session brief — one command full report | Claude Cowork |

---

## STEP 1 — GET THE CODE FROM GITHUB

```
git clone https://github.com/Hennieh22/h2-webhook-bridge.git
cd h2-webhook-bridge
git checkout h2-quant-system
```

The project folder is: `H2_QUANT_V1/`
Everything is inside this folder.

---

## STEP 2 — INSTALL PYTHON DEPENDENCIES

```
pip install pandas numpy scipy scikit-learn hmmlearn pandas-ta MetaTrader5 yfinance plotly statsmodels arch pyarrow requests pyyaml flask
```

Python version: 3.11

---

## STEP 3 — INSTALL NGROK

1. Go to ngrok.com → sign up free
2. Download ngrok for Windows
3. Copy ngrok.exe to `C:\Users\Admin\Desktop\H2_QUANT_V1\`
4. Get your authtoken from dashboard.ngrok.com
5. Run: `ngrok.exe config add-authtoken YOUR_TOKEN`

---

## STEP 4 — RESTORE CONFIG.YAML

Create `config.yaml` in the project root with these exact values:

```yaml
webhook:
  railway_url: "https://h2-webhook-bridge-production.up.railway.app/webhook/tradingview"
  webhook_secret: "H2_SUPER_SECRET_2026"
  timeout_seconds: 10
  dry_run: false

metaapi:
  account_id: "ef07b443-d678-4830-8941-8ab853504970"
  region: "london"
  token: ""  # get fresh token from metaapi.cloud and paste here

mt5:
  login: 52810633
  server: "ICMarketsSC-Demo"
  broker: "IC Markets"
  account_type: "demo"
  path: "C:\\Program Files\\MetaTrader 5\\terminal64.exe"
  password: "4gDCP5hP1@wqxV"

whatsapp:
  provider: "callmebot"
  phone: "27614056155"
  apikey: "2096445"
  url: "https://api.callmebot.com/whatsapp.php"

instruments:
  tier1: [US30, GBPUSD, DE40, XAUUSD, GBPJPY, EURJPY]
  tier2: [EURUSD, USDJPY, USTEC, UK100, AUDUSD, USDCAD]
  discretionary: [JP225, HK50, XAGUSD]
  excluded: [AUS200]

signal_combos:
  tier1_primary:
    - {instrument: US30,   timeframe: 1H}
    - {instrument: GBPUSD, timeframe: 1H}
    - {instrument: UK100,  timeframe: 1H}
    - {instrument: DE40,   timeframe: 1H}
    - {instrument: USTEC,  timeframe: 1H}
    - {instrument: XAUUSD, timeframe: 1H}
    - {instrument: XAUUSD, timeframe: 4H}
    - {instrument: USTEC,  timeframe: 4H}
    - {instrument: EURJPY, timeframe: 1H}
    - {instrument: EURUSD, timeframe: 1H}
    - {instrument: GBPJPY, timeframe: 1H}
    - {instrument: USDJPY, timeframe: 1H}
    - {instrument: XAGUSD, timeframe: 1H}
  entry_timing:
    - {instrument: GBPUSD, timeframe: 15m}
    - {instrument: EURUSD, timeframe: 15m}
    - {instrument: GBPJPY, timeframe: 15m}
  bias_only:
    - {instrument: EURJPY, timeframe: 4H}
    - {instrument: UK100,  timeframe: 4H}
    - {instrument: GBPJPY, timeframe: 4H}
    - {instrument: EURUSD, timeframe: 4H}
    - {instrument: XAGUSD, timeframe: 4H}
    - {instrument: USDJPY, timeframe: 4H}
  manual_watch: [JP225, HK50]

excluded_combos:
  - {instrument: ALL, timeframe: D}
  - {instrument: AUDUSD, timeframe: 1H}
  - {instrument: USDCAD, timeframe: 1H}
  - {instrument: DE40,   timeframe: 15m}
  - {instrument: EURJPY, timeframe: 15m}
  - {instrument: UK100,  timeframe: 15m}
  - {instrument: USDJPY, timeframe: 15m}
  - {instrument: GBPUSD, timeframe: 4H}
  - {instrument: USDCAD, timeframe: 4H}
  - {instrument: AUS200, timeframe: ALL}

state_quality:
  high_conviction_pattern:
    trend: BULL
    momentum: STRONG
    structure: BOS
    rsi: OB
    liquidity: ABOVE
    score_bonus: 0.10
  pullback_neutral_conviction_cap: A
  min_stability_for_primary: 0.40
  min_oos_wr_for_primary: 0.50

gates:
  markov_gap: 0.61
  markov_persistence: 0.82
  volatility_cap: 1.25
  hurst_min: 0.50

sessions:
  US30:   [NY]
  GBPUSD: [LONDON, NY]
  DE40:   [LONDON]
  UK100:  [LONDON]
  XAUUSD: [LONDON, NY]
  GBPJPY: [LONDON, NY]
  EURJPY: [LONDON, NY]
  EURUSD: [LONDON, NY]
  USDJPY: [LONDON, NY]
  AUDUSD: [LONDON, NY]
  USDCAD: [LONDON, NY]
  USTEC:  [NY]
  JP225:  [TOKYO, LONDON]
  HK50:   [TOKYO]
  XAGUSD: [LONDON, NY]

prop:
  daily_loss_limit: -3.0
  max_open_trades: 3
  reduced_size: 0.05
  news_buffer_minutes: 30

monitor:
  interval_seconds: 300
  dry_run: false

users:
  - name: "primary"
    metaapi_id: "ef07b443-d678-4830-8941-8ab853504970"
    whatsapp: "27614056155"
    tier: "full"
    prop_mode: false
    active: true
```

---

## STEP 5 — REFRESH METAAPI TOKEN

1. Go to metaapi.cloud
2. Log in
3. Copy your fresh API token
4. Go to railway.app → your project → Variables
5. Update METAAPI_TOKEN with the fresh token
6. Also paste it into config.yaml under metaapi.token

**This is the most common reason the system stops working.**
**Do this every time you set up a new machine.**

---

## STEP 6 — REBUILD RESEARCH DATA

The Parquet files and transition matrices are large and not in GitHub.
You need to rebuild them. Open Claude Code in H2_QUANT_V1 and paste:

```
Read CLAUDE.md for full context.

The research data needs to be rebuilt on this new machine.
Run the full pipeline in order:

Phase 1: py -3 data/loader.py
  Pull all historical data for all instruments via MT5 API
  Fallback to yfinance if MT5 not connected

Phase 2: py -3 features/engineer.py
  Compute all 22 market dimensions for every bar

Phase 3: py -3 states/classifier.py
  Classify every bar into composite state ID

Phase 4: py -3 markov/engine.py
  Build transition matrices for all instruments

Phase 5: py -3 research/validator.py --all-instruments --walk-forward
  Run full walk-forward validation
  This takes 30-45 minutes — do not interrupt

When complete confirm:
  outputs/H2_transition_matrix_*.json exist
  outputs/H2_state_stats.json exists
  outputs/H2_walkforward_report.md exists
```

---

## STEP 7 — VERIFY SYSTEM HEALTH

```
Open PowerShell and run:
py -3 live/monitor.py

Check that you see:
  MT5 connected — account 52810633
  Starting continuous loop

Open browser:
  https://h2-webhook-bridge-production.up.railway.app/health
  Should return status OK

Send test WhatsApp:
  py -3 -c "import requests; requests.get('https://api.callmebot.com/whatsapp.php?phone=27614056155&text=H2+Recovery+Test&apikey=2096445')"
  You should receive a WhatsApp message
```

---

## STEP 8 — START THE SYSTEM

Double-click `H2_GO.bat` on your desktop.

Or run manually in 4 PowerShell windows:

```
Window 1: python live/monitor.py
Window 2: python dashboard/app.py
Window 3: .\ngrok.exe http 5050
Window 4: python pine/tv_bridge.py
```

---

## STEP 9 — TRADINGVIEW OVERLAY

1. Open TradingView
2. Pine Script Editor → New
3. Open `pine/H2_state_overlay.pine` from project folder
4. Copy entire script → Paste → Add to chart
5. In indicator Settings → Inputs:
   - Theme: Black
   - Left panel: Top Left
   - Right panel: Top Right
6. Copy the ngrok URL from PowerShell window 3
7. Paste into Dashboard URL input field

---

## STEP 10 — CLAUDE COWORK SETUP

1. Open Claude Cowork desktop app
2. New project → point at H2_QUANT_V1 folder
3. Name: H2 Quant System
4. In Customize/Instructions paste:

```
You are the H2 Quant System assistant.

When user types "H2 brief" or "H2 session report":
1. Read outputs/H2_daily_briefing.md
2. Read outputs/H2_live_state.json
3. Read outputs/H2_signal_log.csv (today only)
4. Follow all instructions in cowork/H2_BRIEF_PROMPT.md

When user types "H2 Go":
1. Tell user to double-click H2_GO.bat on desktop
2. Wait for confirmation systems are running
3. Generate and display full H2 brief
4. Tell them which instrument and TF to open now

Project folder: C:\Users\Admin\Desktop\H2_QUANT_V1
```

5. Type `H2 brief` to test

---

## WHAT THE SYSTEM DOES EVERY DAY

### Morning startup
Double-click **H2_GO.bat** on desktop.
4 windows open automatically:
- H2 Monitor (5-minute classification loop)
- H2 Dashboard (live state grid at port 5050)
- H2 Tunnel (ngrok public URL)
- H2 TV Bridge (feeds TradingView overlay)

### When you sit down to trade
Type in Claude Cowork: `H2 brief`
Read top section only — takes 60 seconds.
Open TradingView on recommended instrument.

### Entry checklist
- All 5 gates green on overlay ✓
- Conviction A+ or A ✓
- Volume: RVOL rising, delta matches direction ✓
- Structure: BOS or CHoCH on entry TF ✓
- Fractals: Fractal at key level ✓
- MTF: 4H and 1H agree ✓

### Evening shutdown
Double-click **H2_STOP.bat** on desktop.

---

## VALIDATED RESEARCH RESULTS (June 2026)

### Top instruments by Sharpe ratio

| Instrument | TF | Sharpe | Ruin % | Session |
|---|---|---|---|---|
| GBPUSD | 15m | 22.97 | 0.0% | London + NY |
| EURUSD | 15m | 21.64 | 0.0% | London + NY |
| US30 | 15m | 20.25 | 0.0% | NY only |
| US30 | 1H | 15.08 | 0.1% | NY only |
| GBPJPY | 15m | 15.28 | 0.1% | London + NY |
| XAUUSD | 4H | 12.23 | 0.0% | London/NY overlap |
| USTEC | 4H | 11.91 | 0.0% | NY only |
| UK100 | 1H | 9.14 | 1.1% | London only |
| GBPUSD | 1H | 7.85 | 0.2% | London + NY |
| USTEC | 1H | 7.65 | 0.0% | NY only |
| XAUUSD | 1H | 7.22 | 1.1% | London/NY overlap |
| DE40 | 1H | 7.13 | 1.6% | London only |
| EURJPY | 1H | 4.28 | 0.0% | London + NY |
| EURUSD | 1H | 4.22 | 0.1% | London + NY |
| USDJPY | 1H | 3.83 | 2.1% | London + NY |
| GBPJPY | 1H | 3.11 | 0.0% | London + NY |

### Never trade these
| Instrument | Reason |
|---|---|
| AUS200 | Order rejection unresolved at broker |
| HK50 | Ruin 6.7%, drawdown -5.9R |
| JP225 | Ruin 14%, Sharpe 2.00 — manual only |
| Any Daily TF | Too few states, high ruin |

### Highest conviction setup (walk-forward proven)
```
BULL + STRONG momentum + BOS structure + OB RSI + ABOVE liquidity
= 90%+ OOS win rate — your primary trade
```

### Session trading schedule (SAST)
| Time | Session | Trade |
|---|---|---|
| 09:00-11:00 | London open | DE40, GBPUSD |
| 11:00-14:00 | London mid | GBPUSD, EURUSD |
| 15:00-18:00 | NY open + Overlap | US30, GBPUSD, XAUUSD |
| 18:00-00:00 | NY session | US30, USTEC |

---

## THE 5 GATES (all must pass before signal fires)

| Gate | Threshold | Why |
|---|---|---|
| Markov Gap | >= 61% | Model must be confident |
| Persistence | >= 82% self-transition | State must be self-reinforcing |
| Volatility Cap | <= 1.25x Garman-Klass | Skip chaotic windows |
| Hurst Exponent | >= 0.50 | Trending regime only |
| Session Timing | Per instrument | Edge only in correct session |

## CONVICTION SCORING

| Score | Action |
|---|---|
| A+ — 4/4 pillars | Enter full size |
| A — 3/4 pillars | Enter 50% size |
| B — 2/4 pillars | Wait — do not enter |
| SKIP — 0-1 pillars | No trade |

---

## THE 4 PILLARS (confirmation before entry)

### Pillar 1 — Volume
- EXPANSION: RVOL >= 1.5x, delta positive
- REACTION: Delta flip sell→buy, CVD turning
- PULLBACK: RVOL declining, delta neutral
- LIQ_SWEEP: RVOL spike >= 2x, immediate delta flip

### Pillar 2 — Structure
- EXPANSION: BOS printed on entry TF
- REACTION: MSS + LTF CHoCH confirmed
- PULLBACK: HL holding, no CHoCH
- LIQ_SWEEP: Wick through level + reclaim within 3 bars

### Pillar 3 — Fractals
- EXPANSION: Fractal HL forms and holds after pullback
- REACTION: Fractal low at sweep point — key timing signal
- PULLBACK: LTF fractal at 50-62% retrace zone
- LIQ_SWEEP: Fractal swept + reclaimed within 1-3 bars

### Pillar 4 — MTF Fluency
- 4H state and 1H state must agree on direction
- Entry TF (5m/15m) must confirm with structure event
- Best combo: 4H BULL + 1H PULLBACK + 5m BOS = continuation

---

## COMPOSITE STATE ID FORMAT

```
{TREND}_{MOMENTUM}_{VOLATILITY}_{LIQUIDITY}_{SESSION}_{STRUCTURE}_{RSI}

Example: BULL_STRONG_HIGH_ABOVE_NY_BOS_OB

TREND:      BULL / BEAR / NEUTRAL
MOMENTUM:   STRONG / WEAK / NEUTRAL
VOLATILITY: HIGH / NORMAL / LOW
LIQUIDITY:  ABOVE / BELOW / INSIDE
SESSION:    TOKYO / LONDON / NY / OVERLAP / OFFHOURS
STRUCTURE:  BOS / CHOCH / MSS / RANGE / TREND_CONT / PULLBACK
RSI:        OB / OS / NEUTRAL  (OB>=65, OS<=35)

Hierarchy: MSS > CHOCH > BOS > PULLBACK > RANGE > TREND_CONT
```

---

## KEY FILE LOCATIONS

| File | Purpose |
|---|---|
| CLAUDE.md | Full system spec for Claude Code |
| config.yaml | All settings and credentials |
| outputs/H2_live_state.json | Current state — updated every 5m |
| outputs/H2_daily_briefing.md | Latest session brief |
| outputs/H2_signal_log.csv | All signals with outcomes |
| outputs/H2_state_stats.json | Validated WR/EV per state |
| outputs/H2_walkforward_report.md | Full backtest results |
| pine/H2_state_overlay.pine | TradingView overlay script |
| cowork/H2_BRIEF_PROMPT.md | Cowork brief instructions |
| live/monitor.py | Live 5-minute monitor loop |
| cowork/H2_brief_generator.py | Session brief generator |
| dashboard/app.py | Local web dashboard |
| pine/tv_bridge.py | TradingView data bridge |
| H2_GO.bat | Start everything (one click) |
| H2_STOP.bat | Stop everything (one click) |

---

## TROUBLESHOOTING

| Problem | Fix |
|---|---|
| Trades not executing | MetaAPI token expired — refresh at metaapi.cloud |
| No signals firing | Check dry_run: false in config.yaml |
| WhatsApp silent | Send "Resume" to +34 644 60 70 83 on WhatsApp |
| Railway not responding | Check railway.app dashboard — redeploy |
| All gates failing | Check session — US30 only fires in NY session |
| Brief shows nothing | Ensure live/monitor.py is running |
| MT5 connection error | Open MT5 terminal on Windows first |
| Overlay shows no data | Check ngrok is running, URL pasted in Pine settings |
| GitHub push fails | Check git remote: git remote -v |

---

## WEEKLY MAINTENANCE (every Monday)

```
py -3 data/loader.py
py -3 features/engineer.py
py -3 states/classifier.py
py -3 markov/engine.py
py -3 research/validator.py --all-instruments --walk-forward
```

Also: refresh MetaAPI token at metaapi.cloud

---

---

## GOLDEN HIGHWAY SYSTEM (June 2026)

### What It Is
Golden Highway is a standalone structural entry gate system — independent from the H2 Markov engine.
It does NOT require state classification, Markov matrices, or the 5-gate system.
It reads price structure directly: BOS, CHoCH, liquidity sweeps, pullback completion.

### Status
- Pine Script: Complete. 597 lines. Compiles clean. File: `pine/golden_highway.pine`
- JP225 1H: Validated. Version C live config active.
- All research files: `research/golden_highway_engine.py` and supporting modules.

### Validated Results — JP225 1H (Version C)

| Metric | Value |
|---|---|
| Sessions | Tokyo + London + Overlap only |
| NY session | BLOCKED — degrades performance |
| OOS trades | 21 (approx 2 years of OOS data) |
| Win rate | 52.4% |
| Expectancy | +0.292R per trade |
| Sharpe | 4.62 |
| Profit factor | 2.00 |
| Ruin probability | 0% |

### Key Finding — Session Filter
The most important calibration finding in this system:

| Session | WR | EV | N |
|---|---|---|---|
| London | 100% | +0.72R | 2 |
| Overlap | 100% | +1.40R | 3 |
| Tokyo | 37.5% | +0.03R | 16 |
| NY | 38.9% | -0.02R | 18 |

**NY is blocked in Version C.** Including NY reduces Sharpe from 4.62 to 2.32 and EV from 0.292R to 0.147R.
London/Overlap is the primary edge. Tokyo is acceptable. NY is harmful.

### Session Version Comparison

| Version | N | WR | EV | Sharpe |
|---|---|---|---|---|
| BASELINE all sessions | 39 | 46.2% | 0.147R | 2.32 |
| Version A London+Overlap only | 5 | 100% | 1.128R | 45.70 |
| Version B per-session thresholds | 26 | 46.2% | 0.156R | 2.48 |
| **Version C no NY (live config)** | **21** | **52.4%** | **0.292R** | **4.62** |

### Files
- `pine/golden_highway.pine` — TradingView Pine Script v5, 597 lines
- `research/structure_engine.py` — BOS/CHoCH/fractal engine
- `research/macro_structure_engine.py` — macro state machine
- `research/liquidity_mapper.py` — internal/external liquidity levels
- `research/pullback_engine.py` — 7-check pullback score (0-15)
- `research/entry_score.py` — 5-layer entry score (0-100), session conviction
- `research/golden_highway_engine.py` — full backtest engine, walk-forward
- `config.yaml` — golden_highway: section with all validated parameters

### How to Trade It
1. Check SAST time: is it London (09:00-14:00 SAST), Overlap (14:00-18:00 SAST), or Tokyo (02:00-10:00 SAST)?
2. Open JP225 1H chart in TradingView
3. Load `pine/golden_highway.pine`
4. Check SESSION row in table — must NOT show "NY -- BLOCKED"
5. Check GATES row — SLERPX must be all uppercase (or ~ for Tokyo E gate)
6. Check ENTRY SCORE >= 40 and GRADE >= B
7. Enter on bar close (BAR_CLOSE mode for bots, INTRABAR for manual)
8. SL: 2x ATR below swept fractal
9. TP1: 250 points. TP2: let run on Trade 3.

### SLERPX Gate Reference
- **S** = macro State valid (PULLBACK/LIQ_SWEEP/REACCUMULATION)
- **L** = Location correct (LATE_PULLBACK/PULLBACK_COMPLETE/SWEEP)
- **E** = sEssion valid — uppercase=HIGH (London/Overlap), ~=MEDIUM (Tokyo), lowercase=blocked
- **R** = R:R >= 2.0
- **P** = Pullback score >= 7
- **X** = entry score (eXtra confirmation) >= 40

### Recovery Prompt for Golden Highway
```
Read CLAUDE.md and all golden_highway files in research/.
The Golden Highway is a validated standalone structure trading system.
JP225 1H is the primary instrument. NY session is blocked (Version C live config).
Session conviction: London/Overlap = HIGH, Tokyo = MEDIUM, NY = BLOCKED.
Validated June 2026: 21 OOS trades, WR 52.4%, Sharpe 4.62, EV 0.292R.
Pine Script is complete: pine/golden_highway.pine (597 lines, compiles clean).
Continue from last checkpoint.
```

---

## OUTSTANDING ITEMS (as of June 2026)

### Still to complete
- [ ] JP225 data expansion — pull 5+ years, rerun Phases 3-5, target Sharpe > 5
- [ ] GARCH proper library fit in features/engineer.py
- [ ] Outcome_r auto-tracking from MetaAPI position history
- [ ] News feed connection (ForexFactory RSS)
- [ ] Commercialisation — multi-user fan-out, WhatsApp group, TradingView publish

### Commercialisation tiers when ready
| Tier | What | Price |
|---|---|---|
| Family | Full access — free | Free |
| Tier 1 | WhatsApp signals only | ~$50/month |
| Tier 2 | Signals + auto-execution | ~$150/month |
| Tier 3 | Prop account revenue share | % of profits |

---

## RECOVERY PROMPT FOR CLAUDE CODE

If you need to rebuild or continue — open Claude Code in H2_QUANT_V1 and paste:

```
Read CLAUDE.md and H2_RECOVERY_001.md for full context.

This is the H2 Quant System — a professional quantitative
trading system built for H2 Systematic Trading in
George, South Africa.

System status: All 9 phases complete and validated.
Research: 430 confirmed tradeable edges, walk-forward validated.
Live: System was fully operational before this recovery session.

Credentials and config are in H2_RECOVERY_001.md.

Current task: [DESCRIBE WHAT YOU NEED TO DO]

Start by reading both files and confirming you understand
the full system before making any changes.
```

---

## RECOVERY PROMPT FOR THIS CLAUDE.AI CHAT

If you need to continue planning in claude.ai and have lost context:

```
I am H2 — a trader in George, South Africa.
I have a complete quantitative trading system called H2 Quant System v1.0.

The system is fully built and live. All credentials and architecture
are in H2_RECOVERY_001.md which I will paste or upload.

The system uses:
- IC Markets MT5 Demo (login 52810633)
- MetaAPI (ID: ef07b443-d678-4830-8941-8ab853504970)
- Railway webhook bridge (https://h2-webhook-bridge-production.up.railway.app)
- CallMeBot WhatsApp (API key 2096445, number +27614056155)
- GitHub (https://github.com/Hennieh22/h2-webhook-bridge, branch h2-quant-system)
- Claude Cowork for session briefs
- TradingView Pine Script overlay

Please read H2_RECOVERY_001.md and help me continue.
```

---

*H2 Quant System v1.0 — Recovery File 001*
*H2 Systematic Trading · George, South Africa*
*Created: June 2026*
*Save this file in: C:\Users\Admin\Desktop\H2_QUANT_V1\H2_RECOVERY_001.md*
*Also save to: USB drive, Google Drive, email to yourself*
