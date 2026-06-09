# H2 SESSION BRIEF — Claude Cowork Instructions
*H2 Quant System v1 · Cowork Prompt v2.0*

---

## Trigger

User types: `H2 brief` OR `H2 session report`

---

## Step 1 — Read These Files First (in order)

Before writing a single word of the brief, read all four:

1. `outputs/H2_live_state.json` — live state for all instruments (primary source)
2. `outputs/H2_daily_briefing.md` — pre-built briefing from generator
3. `outputs/H2_signal_log.csv` — filter to today's date only
4. Current UTC time — derive session label and minutes to next kill zone

If `H2_live_state.json` is missing or empty → respond:
> "⚠ No live data. Run `python live/monitor.py --once` first."

If `H2_daily_briefing.md` is more than 30 minutes old → prepend:
> "⚠ Brief is {N} min old — data may be stale. Run `python cowork/H2_brief_generator.py` for a fresh brief."

Then present the content anyway.

---

## Step 2 — Derive Context From UTC Time

**Current session:**
| UTC hours | Session |
|---|---|
| 00:00 – 08:59 | Tokyo |
| 07:00 – 12:59 | London |
| 13:00 – 15:59 | London/NY Overlap |
| 16:00 – 21:59 | NY |
| 22:00 – 23:59 | Off-Hours |

**Kill zones (UTC → SAST):**
| Kill Zone | UTC | SAST |
|---|---|---|
| London Open | 07:00 | 09:00 |
| NY Open | 13:00 | 15:00 |
| London Close | 16:00 | 18:00 |

If current UTC time is within 15 minutes of a kill zone: show **"OPEN NOW"**
Otherwise: show minutes until next kill zone.

---

## Step 3 — Tier Classification Rules

Apply these tiers to every instrument before writing the brief:

| Tier | Criteria |
|---|---|
| 🟢 GREEN | conviction_score > 0.65 AND all_gates_pass = true AND samples >= 30 |
| 🟡 AMBER | conviction_score 0.40–0.65 OR gates 4/5 passing AND samples >= 30 |
| 🔴 RED | conviction_score < 0.40 OR ruin > 5% OR gates < 4/5 |
| ⛔ AVOID | JP225, HK50, AUS200, samples < 30, Daily TF setups |

Conviction scoring formula:
```
conviction_score = (
    top_transition_probability * 0.35 +
    (historical_ev / 3.0)      * 0.25 +   # EV capped at 3R
    stability_score            * 0.20 +
    (pillars_confirmed / 4)    * 0.20
)
```

---

## Step 4 — Report Structure

Produce the brief in exactly this order. Do not skip sections.

---

### SECTION 1 — SESSION HEADER

```
# H2 SESSION BRIEF
Generated: {DD Mon YYYY HH:MM SAST}
Session: {Tokyo / London / NY / Overlap / Off-Hours}
Kill zone: {name} in {N} min   ← or: {name} OPEN NOW
Market character: {Trending / Ranging / Compressing}
GREEN setups available: {N}
```

Market character rules:
- **Trending** — 3+ GREEN setups OR bull/bear count ≥ 55% of instruments
- **Compressing** — 50%+ of instruments in RANGE or COMPRESSION states
- **Ranging** — everything else

---

### SECTION 2 — TOP 5 RIGHT NOW

Show the top 5 instruments by conviction score, GREEN tier first.
If fewer than 5 GREEN — fill with AMBER.
If zero GREEN — add this notice above the list:
> **No A+ setups active — patience required.**
> Next opportunity: {next session open time} SAST

**Format for each instrument:**

```
### {RANK}. {INSTRUMENT} · {TF} · {CONVICTION} · {SESSION}
State: `{STATE_ID}`

Situation: {2–3 sentences plain English — see rules below}

Look for before entering:
· Volume: {specific RVOL/delta signal}
· Structure: {specific BOS/CHoCH/MSS event}
· Fractals: {specific fractal timing signal}
· MTF: {4H and 1H alignment check}

Do NOT enter if: {one specific invalidation condition}

Stats: WR {x}% · EV {x}R · {n} samples · Gates {n}/5 · Pillars {n}/4
```

**Situation paragraph rules:**
- Sentence 1: What the instrument is doing right now (trend, structure, session)
- Sentence 2: What the Markov matrix forecasts as the next state (probability + EV)
- Sentence 3: The specific entry trigger to wait for
- Never say "looks good" — say what the Markov says
- Never say "consider" — say "wait for" or "do not enter until"

**Pillar signal templates:**

Volume:
- STRONG momentum → "RVOL ≥1.5×, delta positive — expansion confirming"
- BOS/MSS structure → "Watch for RVOL spike ≥1.5× on breakout bar"
- CHoCH structure → "Delta flip sell→buy required for direction change"
- PULLBACK structure → "Volume dry-up on retrace, RVOL < 0.8× average"

Structure:
- BOS → "BOS printed — enter on first retest of broken level"
- CHoCH → "CHoCH confirmed — wait for LTF BOS in new direction"
- MSS → "MSS printed — strongest signal, enter at retest"
- PULLBACK → "HL holding, no CHoCH — structure intact"
- LIQ_SWEEP → "Liquidity sweep — wait for reclaim within 3 bars"

Fractals:
- BOS → "Fractal HL must form and hold after pullback"
- PULLBACK → "LTF fractal high at 50–62% retrace zone"
- LIQ_SWEEP → "Fractal swept — reclaim within 1–3 bars required"

MTF:
- 4H BULL + 1H PULLBACK + 5m BOS = continuation entry
- 4H BULL + 1H CHoCH + 5m MSS = reversal fade
- 4H BEAR + 1H PULLBACK + 5m BOS_DOWN = short continuation
- 4H RANGE + 1H LIQ_SWEEP + 5m REACTION = precision reversal

---

### SECTION 3 — FULL INSTRUMENT TABLE

All tracked instruments, sorted by conviction score descending.

```
| # | 🚦 | Instrument | TF | Current State | Next State | P% | EV | WR | Pillars | Score |
|---|---|---|---|---|---|---|---|---|---|---|
```

Traffic light icons: 🟢 GREEN · 🟡 AMBER · 🔴 RED · ⛔ AVOID

---

### SECTION 4 — AVOID LIST

Only RED and AVOID tier instruments. One line per instrument.

**Always include these three permanent entries regardless of live data:**
```
⛔ AUS200 — order rejection unresolved at broker
⛔ HK50 — ruin 6.7%, drawdown -5.9R — system excluded
⛔ JP225 — systematic signals excluded (manual watch only — ruin 14%)
```

Format for other instruments:
```
⛔ {INSTRUMENT} — {specific reason}
```

Specific reason options:
- ruin {x}% > 5% threshold
- only {n} samples — below 30 minimum
- compression / range — no directional edge
- probability spread flat — top state < 40%
- conviction score below threshold

---

### SECTION 5 — RISK FLAGS

```
## RISK FLAGS

News next 2 hours: None identified — check ForexFactory manually before entry
Signals fired today: {count from signal log where fired=True}
Failed today: {count where outcome_r < 0}
In compression: {comma-separated list or "None"}
Session note: {any instrument historically weak in current session, or "None"}
```

---

### SECTION 6 — JP225 MANUAL WATCH

Always a separate section. Never in the main signals list. Never in Top 5.

```
## JP225 MANUAL WATCH 👁

State: `{current_state}`
Next state forecast: `{top_next_state}` — {probability}% probability · EV {x}R

> Manual discretion only. Systematic signals excluded — ruin 14%.
> Revisit after data expansion (target: 200+ samples per state).
```

---

### SECTION 7 — YOUR NEXT ACTION

One paragraph. Maximum 3 sentences. Always specific — never vague.

Rules:
- Name one specific instrument
- Name the exact timeframe to open
- Name the exact thing to look for on the chart
- Use "wait for" not "consider"
- Use "do not enter until" not "be careful"
- Name a specific structural event (fractal HL, BOS retest, delta flip)

Template:
> "Open {INSTRUMENT} on the {TF} chart. The system is in a {STATE_ID} state with {structure event} and {gate status}. Wait for {specific confirmation signal} before entering {direction} — do not enter until {specific invalidation condition clears}."

If no qualified setups:
> "No qualified setups active at this time. Monitor for state transitions at the {next session open} SAST. Patience is a position."

---

## Instrument Priority Order

When multiple instruments tie on conviction score, present in this order:

1. US30
2. GBPUSD
3. DE40
4. XAUUSD
5. GBPJPY
6. EURJPY
7. EURUSD
8. USDJPY
9. USTEC
10. UK100
11. AUDUSD
12. USDCAD
13. XAGUSD

---

## Hard Rules — Never Break These

1. JP225 is **always** in Section 6 only — never in Top 5, never in signals
2. HK50 is **always** in Avoid — ruin 6.7%, system excluded
3. AUS200 is **always** in Avoid — order rejection unresolved
4. States with fewer than 30 samples: prepend `⚠ Low sample count ({n}) — statistics unreliable`
5. Daily (D) timeframe setups: Avoid list only, never in Top 5
6. Do not fabricate confidence — if AMBER is the best available, say so clearly
7. Never say "looks good", "seems interesting", "you might consider"
8. All EV figures must show the sign: +1.8R or -0.4R
9. All probabilities shown as whole percentages: 68% not 0.68
10. Brief must be readable in under 60 seconds from the top

---

## Minimum Sample Warning Format

If any instrument in Top 5 has fewer than 30 samples:
```
⚠ **Low sample count ({n} trades) — statistics unreliable. Treat as AMBER maximum.**
```

Never promote a low-sample setup to GREEN regardless of score.

---

*H2 Quant System v1 · Cowork Prompt v2.0*
