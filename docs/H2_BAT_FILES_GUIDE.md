# H2 Trading System — Batch Files Reference Guide

## Quick Reference

| Bat File | When to Run | What it Does |
|---|---|---|
| trade.bat | Start of each session | Generates full trading brief |
| scan.bat | Every 30 min during session | Live destination ladder scan |
| monitor.bat | Leave running all session | Background scan + WhatsApp alerts |
| backup.bat | Manual backup anytime | Full project backup |
| auto_trade.bat | Run by Task Scheduler | Automated brief (do not run manually) |

---

## Detailed Guide

### trade.bat
**When:** Run once at the start of each trading session (London open, NY open)

**What it does:**
- Fetches live macro data (VIX, DXY, TNX, FX rates)
- Calls Claude API to research current geopolitical events via web search
- Generates a comprehensive trading brief covering:
  - Session state (FLOW/TRANSITION/SHOCK)
  - Geopolitical assessment per instrument
  - Economic calendar for next 48 hours
  - Ranked opportunities (all 15 instruments scored)
  - Top 5 setups with entry/stop/target levels
  - Destination ladders
  - Avoid list
  - Panel 2 and Panel 3 update instructions
- Saves brief to `outputs/briefs/H2_brief_YYYYMMDD_HHMM.md`
- Saves extracted news values to `outputs/h2_news_state.json`
- Writes TV update instructions to `outputs/h2_news_update_instruction.txt`

**What to do with the output:**
1. Read the SESSION STATE — if SHOCK, size down 25%
2. Check AVOID TODAY list first — don't trade those instruments
3. Copy Panel 2 values into H2·NEWS settings if geo risk changed
4. Copy Panel 3 values into H2·VERDICT settings
5. Focus on TOP 5 SETUPS — these are your primary watchlist

---

### scan.bat
**When:** Run every 15-30 minutes during active trading session

**What it does:**
- Fetches live FX prices (exchangerate-api.com)
- Fetches live gold/silver prices (gold-api.com)
- Reads live VWAP destinations from Railway (populated by TradingView broadcasters)
- Computes destination ladder for all 15 instruments
- Ranks by EV (expected value) using validated hit rates:
  - JP225: 60% D1 hit rate
  - USTEC: 37% D1 hit rate
  - All others: 45% D1 hit rate
- Applies session gates (London/NY/Asia appropriate instruments only)
- Fires WhatsApp alert if a destination flip is detected
- Saves results to `outputs/h2_scan_latest.json`

**What to do with the output:**
1. Look at SRC column — LVE = live data, ATR = estimated (less reliable)
2. Focus on top 3 instruments by EV in your current session
3. If WhatsApp fires a flip alert — check that instrument on the chart immediately
4. Cross-reference with Panel 4 (H2·MTF) TIME-DISTANCE section

---

### monitor.bat
**When:** Start at session open, leave running in background

**What it does:**
- Runs scan automatically every 15 minutes
- No manual intervention needed
- Only scans during London and NY sessions (not Asia, unless you trade Asia)
- Fires WhatsApp when destination flip detected

**What to do with the output:**
- Keep the window minimized
- Act on WhatsApp alerts as they come
- Check the terminal window if you want to see current state

---

### backup.bat
**When:** Anytime you want a manual backup (in addition to the 2x daily automatic ones)

**What it does:**
- Copies all project files to `C:\Users\Admin\H2_Backups\H2_Backup_YYYY-MM-DD_HHMM\`
- Creates a git bundle (complete repo history in one portable file)
- Writes a BACKUP_MANIFEST.json with file count and size
- Keeps last 10 backups, deletes older ones

**What to do with the output:**
- Nothing — just confirm a new folder appeared in H2_Backups
- If you ever need to restore: the git bundle contains the complete project history

---

### auto_trade.bat
**When:** DO NOT run manually — triggered by Task Scheduler automatically

**Schedule:** 02:00, 06:00, 10:00, 14:00, 18:00, 22:00 SAST

**What it does:**
- Same as trade.bat but runs silently
- Logs to `outputs/auto_brief_log.txt`
- Generates news state and writes instruction for Cowork to update H2·NEWS

---

## Task Scheduler — What's Running Automatically

| Task Name | Time (SAST) | Bat File | Purpose |
|---|---|---|---|
| H2_AutoBrief_0200 | 02:00 | auto_trade.bat | Asia session brief |
| H2_AutoBrief_0600 | 06:00 | auto_trade.bat | Pre-London brief |
| H2_AutoBrief_1000 | 10:00 | auto_trade.bat | London mid brief |
| H2_AutoBrief_1400 | 14:00 | auto_trade.bat | NY open brief |
| H2_AutoBrief_1800 | 18:00 | auto_trade.bat | NY mid brief |
| H2_AutoBrief_2200 | 22:00 | auto_trade.bat | Asia prep brief |
| H2 Backup Morning | 11:00 | backup.bat | Morning backup |
| H2 Backup Evening | 20:00 | backup.bat | Evening backup |

---

## Output Files — What They Mean

| File | Location | Updated by | Use |
|---|---|---|---|
| H2_brief_*.md | outputs/briefs/ | trade.bat | Full session brief — read at session start |
| h2_scan_latest.json | outputs/ | scan.bat | Latest scan results — machine readable |
| H2_live_state.json | outputs/ | broadcasters via Railway | Live VWAP destinations — read by scan |
| h2_news_state.json | outputs/ | trade.bat | Extracted news values — for H2·NEWS panel |
| auto_brief_log.txt | outputs/ | auto_trade.bat | Automated brief log — check if something seems off |
| h2_news_update_instruction.txt | outputs/ | trade.bat | Step-by-step TV update instructions |
