# H2 Live State Alert — TradingView Setup Procedure (Verified Working)

## Background
H2_MTF_v1.pine uses `alert()` (not `alertcondition()`) to push live VWAP
destination data to Railway on every bar close. This required fixing
a Pine v6 compile error where `alertcondition()`'s message parameter
must be a `const string`, but our payload is dynamic (`series string`).

## The working Pine code (already saved in pine/H2_MTF_v1.pine)
At the end of the script, after the existing `plot(na)`:

```pine
live_state_json = '{"type":"live_state"' +
     ',"symbol":"'       + syminfo.ticker + '"' +
     ',"dest_1h":'       + str.tostring(dest_1, "#.##") +
     ',"dir_1h":"'       + (dir_1 == "BULL" ? "UP" : "DOWN") + '"' +
     ',"dest_4h":'       + str.tostring(dest_4, "#.##") +
     ',"dir_4h":"'       + (dir_4 == "BULL" ? "UP" : "DOWN") + '"' +
     ',"dest_d":'        + str.tostring(dest_d, "#.##") +
     ',"dir_d":"'        + (dir_d == "BULL" ? "UP" : "DOWN") + '"' +
     ',"regime":"'       + regime_4 + '"' +
     ',"journey_state":' + str.tostring(journey_state) +
     ',"timestamp":'     + str.tostring(math.round(time / 1000)) +
     '}'

if barstate.isconfirmed
    alert(live_state_json, alert.freq_once_per_bar_close)
```

## Alert creation procedure (manual, per instrument)

For EACH instrument (JP225, USTEC, XAUUSD, DE40, UK100, USDJPY, XAGUSD):

1. Switch the chart symbol to the target instrument
2. Confirm H2 MTF v1.0 (updated version) is loaded on the chart
3. Click the Alert (clock) icon
4. **Condition** dropdown → "H2·MTF (...)"
5. Second dropdown → will auto-show **"Any alert() function call"**
   (this is correct — do not change it)
6. **Interval** → "Same as chart" (alert fires on chart's own timeframe bar close)
7. **Message field** → clear any default text, type exactly:

```
{{message}}
```

   This placeholder passes through the literal `live_state_json` string
   built in the Pine script. Do NOT leave TradingView's auto-generated
   summary text in this field — it must be `{{message}}` or genuinely empty.
8. **Expiration** → Open-ended
9. **Notifications** → enable ONLY "Webhook URL"
   (disable App/Toasts/Email/Sound — this fires every bar close,
   too frequent for those channels)
10. **Webhook URL** field — paste EXACTLY:

```
https://h2-webhook-bridge-production-872e.up.railway.app/live_state
```

    CRITICAL: this is NOT the same URL as the trade execution webhook
    (`/webhook/tradingview`). Confirm the path is `/live_state` and the
    domain includes the `-872e` suffix.
11. **Alert name** → `H2 Live State - {SYMBOL}` (e.g. "H2 Live State - JP225")
12. Click **Create**

## Verification
After one bar close on the chart's timeframe, check:

```
https://h2-webhook-bridge-production-872e.up.railway.app/live_state
```

The instrument should appear with populated dest_1h, dest_4h, dest_d,
dir_1h, dir_4h, dir_d, regime, journey_state, and timestamp fields.

## Instruments to set up (in priority order)
- [x] JP225 — alert created, awaiting first bar close verification
- [ ] USTEC
- [ ] XAUUSD
- [ ] DE40
- [ ] UK100
- [ ] USDJPY
- [ ] XAGUSD

## Common mistakes (from initial setup attempt)
1. Using `alertcondition()` instead of `alert()` — causes
   "const string expected" compile error since the message is dynamic
2. Leaving the old webhook URL (`/webhook/tradingview` without `-872e`)
   — this is the TRADE EXECUTION endpoint, not live state
3. Leaving TradingView's auto-generated condition summary in the
   Message field instead of `{{message}}` — sends the wrong payload
4. Creating duplicate `plot(na)` lines when inserting the alert code —
   harmless but should be cleaned up (only one plot(na) needed)
