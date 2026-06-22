@echo off
:: H2 Auto Trade Brief — for Task Scheduler unattended runs
:: Runs brief + news updater, logs to outputs/auto_brief_log.txt
:: Triggered 6x daily: 02:00 06:00 10:00 14:00 18:00 22:00 SAST
::
:: API keys must be set as Windows System Environment Variables (not here).
:: System Properties → Advanced → Environment Variables:
::   ANTHROPIC_API_KEY  = sk-ant-api03-...
::   FMP_API_KEY        = p78jbm...
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d C:\Users\Admin\Desktop\H2_QUANT_V1

echo [%DATE% %TIME%] Auto brief started >> outputs\auto_brief_log.txt
python cowork\h2_trade_brief.py  >> outputs\auto_brief_log.txt 2>&1
python cowork\h2_news_updater.py >> outputs\auto_brief_log.txt 2>&1
echo [%DATE% %TIME%] Auto brief complete >> outputs\auto_brief_log.txt
