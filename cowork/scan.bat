@echo off
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d C:\Users\Admin\Desktop\H2_QUANT_V1
set FMP_API_KEY=p78jbm6tzFGhlDzijcmybkqJuNBeXFlv
set RAILWAY_URL=https://h2-webhook-bridge-production.up.railway.app
set WA_PHONE=27614056155
set WA_APIKEY=2096445
python cowork\h2_scan.py >> outputs\h2_scan_log.txt 2>&1
python cowork\h2_scan.py
pause
