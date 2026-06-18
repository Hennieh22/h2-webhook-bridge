@echo off
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d C:\Users\Admin\Desktop\H2_QUANT_V1
set FMP_API_KEY=p78jbm6tzFGhlDzijcmybkqJuNBeXFlv
set RAILWAY_URL=https://h2-webhook-bridge-production.up.railway.app
set WA_PHONE=27614056155
set WA_APIKEY=2096445
set H2_SCAN_INTERVAL=900
echo H2 Monitor starting -- scans every 15 minutes
echo Press Ctrl+C to stop
python cowork\h2_monitor.py
