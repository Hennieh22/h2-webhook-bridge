@echo off
:: API keys are loaded from Windows Environment Variables — set them once in System Properties.
:: To set permanently: System Properties → Advanced → Environment Variables
::   ANTHROPIC_API_KEY  = sk-ant-api03-...
::   FMP_API_KEY        = p78jbm...
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d C:\Users\Admin\Desktop\H2_QUANT_V1
python cowork\h2_trade_brief.py > cowork\trade_output.txt 2>&1
python cowork\h2_news_updater.py >> cowork\trade_output.txt 2>&1
type cowork\trade_output.txt
