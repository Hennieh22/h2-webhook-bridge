# H2 Cowork Commands

## Setup
This project folder must be opened in Claude Cowork (desktop tool).

Environment variables required in your system or .env file:
- ANTHROPIC_API_KEY = your Claude API key
- FMP_API_KEY = p78jbm6tzFGhlDzijcmybkqJuNBeXFlv
- RAILWAY_URL = https://h2-webhook-bridge-production-872e.up.railway.app

## Commands

### "trade"
Runs the complete pre-session intelligence brief.
Takes ~60 seconds. Produces:
- Full terminal report with all 15 instruments ranked
- Saved markdown file in outputs/briefs/
- Geo state update for Panel 2

Cowork instruction: When I say "trade", run:
python cowork/h2_trade_brief.py

### "verdict [INSTRUMENT]"
Example: "verdict JP225"
Updates Panel 3 inputs for the specified instrument.
Cowork instruction: When I say "verdict [X]", read the latest brief
in outputs/briefs/ and extract the Panel 3 values for instrument X,
then display exactly what to paste into Panel 3 settings.

### "news check"
Quick news status without full brief.
Cowork instruction: When I say "news check", fetch
https://h2-webhook-bridge-production-872e.up.railway.app/news/status
and display per-instrument status in a clean table.

### "ladder [INSTRUMENT]"
Example: "ladder XAUUSD"
Shows the destination ladder for one instrument.
Cowork instruction: When I say "ladder [X]", read the latest brief
and display the destination ladder section for instrument X.

### "scan"
Shows only the ranked opportunity table from the latest brief.
Cowork instruction: When I say "scan", read the latest brief
and display only the RANKED OPPORTUNITIES and TOP 5 SETUPS sections.

---

## Auto News Update — After "trade" command

After `trade.bat` runs (manually or via Task Scheduler), the pipeline
automatically:
1. `h2_trade_brief.py` generates the brief AND writes `outputs/h2_news_state.json`
2. `h2_news_updater.py` reads that JSON and writes `outputs/h2_news_update_instruction.txt`

### Cowork: after running "trade", update the news panel
When I say "trade" and the brief has been generated, Cowork should:
1. Read `outputs/h2_news_update_instruction.txt`
2. Follow the step-by-step computer-use instructions in that file to update
   the H2·NEWS·v2 indicator settings in TradingView.
3. Report the final Panel 2 status line shown on chart (e.g. "CAUTION | GEO: ELEVATED").

If TradingView is not open, open it in Microsoft Edge first, then apply.

### Task Scheduler (automated 6x daily)
`auto_trade.bat` is registered in Windows Task Scheduler to run at:
  02:00, 06:00, 10:00, 14:00, 18:00, 22:00 SAST

Log file: `outputs/auto_brief_log.txt`
Instruction file (for next Cowork session): `outputs/h2_news_update_instruction.txt`

To check the latest auto-run result:
  - Read `outputs/auto_brief_log.txt` (last 30 lines)
  - Read `outputs/h2_news_state.json` for extracted values
  - Read `outputs/h2_news_update_instruction.txt` for the pending TradingView update
