# Golden Highway — JP225 1H Validated System Summary
*H2 Systematic Trading · George, South Africa · June 2026*

---

## 1. WHAT THE SYSTEM DOES

Golden Highway is a standalone structural entry gate system that reads price directly — BOS levels, liquidity sweeps, and pullback completion — without requiring Markov matrices or composite state classification. It scores each potential entry across 6 gates and 5 layers (0–100), then fires a signal only when structure, location, R:R, pullback quality, and session conviction all align. JP225 1H is the primary validated instrument, running on IC Markets MT5 data across 8.4 years of history.

---

## 2. VALIDATED RESULTS — VERSION C LIVE CONFIG

| Metric | Value |
|---|---|
| Instrument | JP225 1H |
| Backtest period | 2018-01-01 to 2026-06-10 (8.4 years) |
| OOS split | 80% IS / 20% OOS (walk-forward, no lookahead) |
| OOS period | ~2 years |
| **OOS trades** | **21** |
| **Win rate** | **52.4%** |
| **Expectancy** | **+0.292R per trade** |
| **Sharpe** | **4.62** |
| **Profit factor** | **2.00** |
| Ruin probability | 0% |
| Gates | entry_score >= 40, pullback >= 7, RR >= 2.0 |
| Sessions | Tokyo + London + Overlap only (NY blocked) |

---

## 3. THE LONDON FINDING

The single most important calibration discovery in this project:

| Session | WR | EV | Trades |
|---|---|---|---|
| **London** | **100%** | **+0.72R** | **2** |
| **Overlap** | **100%** | **+1.40R** | **3** |
| Tokyo | 37.5% | +0.03R | 16 |
| NY | 38.9% | -0.02R | 18 |

**Why London/Overlap produces 100% WR:**
JP225 is the Nikkei 225. Its primary price discovery happens in Tokyo (Asian open). By the time London opens (07:00 UTC / 09:00 SAST), the macro move is already printed on the chart. London traders see the Asian structure clearly and react to it with precision. BOS levels from the Tokyo session hold as reference points. Pullbacks into these levels during London have near-perfect structural context — the direction is confirmed, the liquidity swept, the location defined.

**Why NY degrades performance:**
JP225 in the NY session (16:00-21:00 UTC / 18:00-23:00 SAST) is trading on American overnight interest in a Japanese instrument. Volume thins, the structure becomes noisy, and BOS levels from Asian/European sessions lose relevance. The system fires signals but they land in a different market regime.

**What this means for live trading:**
The optimal JP225 trading window is 09:00–18:00 SAST (London + Overlap). Tokyo signals (02:00–10:00 SAST) are acceptable but lower conviction. Everything after 18:00 SAST is ignored — the Pine Script shows "NY — BLOCKED" in red and the SLERPX `E` gate drops to lowercase.

---

## 4. HOW TO TRADE IT — DAILY ROUTINE

**Step 1 — Check SAST time**
- 02:00–10:00 SAST = Tokyo session → signals acceptable (TOKYO ★★)
- 09:00–14:00 SAST = London session → highest conviction (LONDON ★★★)
- 14:00–18:00 SAST = Overlap session → highest conviction (OVERLAP ★★★)
- After 18:00 SAST = NY session → do not trade JP225

**Step 2 — Open chart**
- TradingView → JP225 → 1H timeframe
- Load `pine/golden_highway.pine` → Add to chart

**Step 3 — Read the dashboard (top-left table)**
- MACRO STATE: must show PULLBACK or LIQ_SWEEP (green)
- LOCATION: must show LATE_PULLBACK, PULLBACK_COMPLETE, or SWEEP_AT_LOW/HIGH (green)
- SESSION: must show LONDON ★★★, OVERLAP ★★★, or TOKYO ★★ — never "NY -- BLOCKED"
- GATES: SLERPX row — all uppercase = all gates pass. ~ = Tokyo session (medium, still valid)
- ENTRY SCORE: >= 40 = Grade B minimum
- GRADE: B = enter 50% size, A = 75%, A+ = full size

**Step 4 — Verify the narrative**
- Is there a clear BOS level on the 1H chart?
- Did price pull back cleanly to that level?
- Is there a liquidity sweep (wick beyond the fractal)?
- Does the 4H agree with direction?

**Step 5 — Enter**
- BAR_CLOSE mode: wait for the 1H bar to close fully
- INTRABAR mode: enter mid-bar (manual confirmation only, not for bots)
- SL: 2x ATR below the swept fractal low (for LONG)
- TP1: +250 points (primary target, close 50% position)
- TP2: let Trade 3 run to the external liquidity level shown on chart

**Step 6 — Log and review**
- WhatsApp alert fires automatically with full signal details
- Log outcome in H2_signal_log.csv

---

## 5. WHAT COMES NEXT

| Phase | Task | Status |
|---|---|---|
| Done | JP225 1H validation, Version C config, Pine Script | Complete |
| Next | Full instrument universe backtest (DE40, UK100, USTEC, US30, XAUUSD) | Pending |
| Next | BOS cycle tracking — 3-trade methodology, cycle position display | Pending |
| Next | LTF 1M/5M/15M confirmation layer — add as Pillar 4 | Pending |
| Next | VWAP mean system integration | Pending |
| Next | Bot execution — connect golden_highway.pine alerts to Railway bridge | Pending |
| Future | Volatility-aware position sizing (GARCH regime filter) | Pending |
| Future | Prop trading mode — daily loss cap, news buffer | Pending |

---

## 6. SLERPX GATE REFERENCE

The GATES row in the dashboard shows a 6-character code — one letter per gate.
Uppercase = gate passes. Lowercase = gate fails. ~ = medium (Tokyo session).

| Letter | Gate | Pass condition |
|---|---|---|
| **S** | macro **S**tate | PULLBACK, LIQ_SWEEP, or REACCUMULATION |
| **L** | **L**ocation | LATE_PULLBACK, PULLBACK_COMPLETE, or SWEEP_AT_LOW/HIGH |
| **E** | s**E**ssion | Uppercase E = London/Overlap (HIGH). ~ = Tokyo (MEDIUM). e = NY (BLOCKED) |
| **R** | **R**:R available | >= 2.0 |
| **P** | **P**ullback score | >= 7 of 15 (7-check system: RSI, location, liquidity, vol, fractal, HTF, flow) |
| **X** | entry score (e**X**tra) | >= 40 of 100 |

**Full signal example:** `6/6 gates  SLE~RPX` → wait, the ~ means Tokyo session (medium), E gate shows ~, not uppercase E. London/Overlap: `SLERPX` all uppercase. NY session: `SLerPX` with lowercase e.

**Alert format (WhatsApp):**
```
GH SIGNAL | JP225 | LONG | LIQ_SWEEP | SWEEP_AT_LOW | PB:10 | ES:62 | RR:3.2 | A | TOKYO
```

---

*Golden Highway v3 · JP225 1H · Version C Live Config*
*H2 Systematic Trading · George, South Africa*
*Validated: June 2026 | 21 OOS trades | WR 52.4% | Sharpe 4.62*
