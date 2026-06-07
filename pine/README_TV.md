# H2 State Overlay — TradingView Installation Guide

## Overview
The H2 State Overlay is a Pine Script v5 display indicator that shows the
H2 Quant live state directly on your TradingView chart. It has four panels:

- **Panel 1 (top-left):** Current state ID + plain English description
- **Panel 2 (below Panel 1):** Top 3 next states with probabilities and EV
- **Panel 3 (top-right):** 5-gate status panel (✓/✗ per gate)
- **Panel 4 (below Panel 3):** Conviction rating (A+/A/B/SKIP) + stats

---

## Quick Start — Test Version (5 minutes)

1. Open TradingView → Pine Editor (bottom tab)
2. Delete all existing code
3. Open `pine/H2_state_overlay_TEST.pine` in any text editor
4. Copy all contents
5. Paste into TradingView Pine Editor
6. Click **Add to chart**

You should see:
- Left panel: `BULL_STRONG_HIGH_ABOVE_NY_BOS_OB` (JP225 test state)
- Right panel: All 5 gates PASS, Conviction A+, Gold background tint
- Next states: 68% / 21% / 11% probability bars

---

## Live Setup — Production Version

### Step 1: Add the main indicator

1. Pine Editor → paste contents of `pine/H2_state_overlay.pine`
2. Click **Add to chart**
3. The panels will show "No data" / "UNKNOWN" — this is normal

### Step 2: Get current values from the TV bridge

Run the bridge script:
```bash
python pine/tv_bridge.py --instrument JP225
```

This prints all current input values in the correct format.

### Step 3: Paste values into indicator settings

1. Click the ⚙️ (settings) icon on the H2 State Overlay indicator
2. In the **Inputs** tab, paste each value from the bridge output
3. Click **OK**

The overlay updates immediately.

### Step 4: Auto-generate updated Pine files

```bash
python pine/tv_bridge.py --instrument JP225 --generate-pine
```

This creates `pine/H2_state_overlay_JP225_LIVE.pine` with the current values
hardcoded. Paste this into TradingView whenever the state changes.

### Step 5: Continuous loop

```bash
python pine/tv_bridge.py --instrument JP225 --loop
```

Prints updated values every 5 minutes. Re-paste the generated .pine file
into TradingView after each significant state change.

---

## Input Field Reference

| Input | Description | Example |
|---|---|---|
| Current State ID | Full composite state string | `BULL_STRONG_HIGH_ABOVE_NY_BOS_OB` |
| Plain English Description | Human-readable description | `JP225: bullish with strong momentum...` |
| Session | Current trading session | `NY` |
| SAST Time | Current time in SAST | `17:45` |
| Bars in State | How long in current state | `4` |
| Avg Dwell Time | Historical avg bars per state | `6.2` |
| Next State 1/2/3 | Top 3 next state IDs | `BULL_WEAK_NORMAL...` |
| Probability 1/2/3 | Transition probability (0–1) | `0.68` |
| EV 1/2/3 | Expected value in R | `1.80` |
| Gate 1–4 values | Numeric gate values | `0.68`, `0.87`, `1.05`, `0.61` |
| Gate 5 Session Pass | Boolean | `true` |
| Conviction | Rating | `A+` |
| Pillars | 0–4 | `4` |
| Historical WR | Win rate (0–1) | `0.71` |
| Historical EV | Expected value | `1.80` |
| Sample Count | Number of observations | `847` |

---

## Color Reference

| Color | Meaning |
|---|---|
| Gold `#ffd700` | A+ conviction, header text |
| Green `#26a69a` | Bullish / gate pass / A |
| Red `#ef5350` | Bearish / gate fail / SKIP |
| Orange `#ff9800` | Neutral / warning / B |
| Purple `#7c4dff` | State labels, panel borders |
| Gray `#787b86` | Inactive / low probability |

---

## Panel Layout

```
┌────────────────────────────┐  ┌──────────────────────────────┐
│ H2 QUANT    LIVE STATE     │  │ GATE    STATUS  VALUE  NEED  │
│ STATE: BULL_STRONG_HIGH... │  │ Markov Gap  ✓   68%   ≥61%  │
│ DESC:  JP225 bullish...    │  │ Persistence ✓   87%   ≥82%  │
│ DWELL: Bar 4 / avg 6.2     │  │ Vol Cap     ✓   1.05× ≤1.25×│
├────────────────────────────┤  │ Hurst       ✓   0.61  ≥0.50 │
│ NEXT STATES  PROB    EV    │  │ Session     ✓   NY    OK    │
│ BULL_WEAK... 68%  ▲1.80R  │  ├──────────────────────────────┤
│ BULL_STRO... 21%  ▲2.10R  │  │    ALL GATES PASS            │
│ BEAR_WEAK... 11%  ▼0.40R  │  ├──────────────────────────────┤
└────────────────────────────┘  │    CONVICTION                │
                                │         A+                   │
                                │  ◆ ◆ ◆ ◆  4/4 pillars       │
                                │  WR    EV    Samples  Stab   │
                                │  71%  +1.8R  847      0.84   │
                                └──────────────────────────────┘
```

---

## Publishing as Invite-Only Script

1. In TradingView Pine Editor, click **Publish Script**
2. Set visibility to **Invite-only**
3. Add description referencing H2 Quant System
4. Share invite link only with authorised users

The copyright block at the top of the script enforces this:
```pine
// © 2026 H2 Quant Systems
// Invite-only — do not redistribute
```

---

## Files

| File | Description |
|---|---|
| `H2_state_overlay.pine` | Production indicator with input fields |
| `H2_state_overlay_TEST.pine` | Hardcoded test (JP225 A+ scenario) |
| `H2_state_overlay_{INST}_LIVE.pine` | Auto-generated live version |
| `tv_bridge.py` | Bridge script — formats values for TV inputs |
| `README_TV.md` | This file |

---

*H2 Quant v1 — Phase 8 | © 2026 H2 Quant Systems*
