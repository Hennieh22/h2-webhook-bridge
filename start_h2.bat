@echo off
:: ============================================================
:: H2 Quant v1 — Full Stack Startup
:: Run this once to start the entire H2 system.
::
:: Start order:
::   1. ngrok        — exposes dashboard to TradingView / phone
::   2. Dashboard    — Flask server on localhost:5050
::   3. Instructions — how to start the monitor + Pine setup
:: ============================================================

title H2 Quant — Startup

echo.
echo ============================================================
echo   H2 QUANT v1 — SYSTEM STARTUP
echo ============================================================
echo.

:: ── Step 1: Check ngrok exists ───────────────────────────────
if not exist "ngrok.exe" (
    echo [ERROR] ngrok.exe not found in %CD%
    echo.
    echo   To install ngrok:
    echo   1. Go to https://ngrok.com/download
    echo   2. Download ngrok for Windows
    echo   3. Extract ngrok.exe to: %CD%\
    echo   4. Run: ngrok config add-authtoken YOUR_TOKEN_HERE
    echo      ^(get token from https://dashboard.ngrok.com/get-started/your-authtoken^)
    echo   5. Run this batch file again
    echo.
    pause
    exit /b 1
)

:: ── Step 2: Start ngrok in a new window ─────────────────────
echo [1/3] Starting ngrok tunnel on port 5050...
start "H2 ngrok" cmd /k "ngrok http 5050"

:: Wait for ngrok to establish the tunnel before dashboard starts
echo       Waiting 4 seconds for tunnel to establish...
timeout /t 4 /nobreak > nul

:: ── Step 3: Start dashboard server ──────────────────────────
echo [2/3] Starting H2 Dashboard server on port 5050...
start "H2 Dashboard" cmd /k "cd /d %~dp0 && py -3 dashboard\app.py"

:: Wait for Flask to start
timeout /t 3 /nobreak > nul

:: ── Step 4: Query ngrok URL and display it ──────────────────
echo [3/3] Detecting ngrok public URL...
echo.

py -3 -c "
import urllib.request, json, sys, time
time.sleep(1)
try:
    with urllib.request.urlopen('http://localhost:4040/api/tunnels', timeout=5) as r:
        data = json.loads(r.read())
    url = None
    for t in data.get('tunnels', []):
        if t.get('proto') == 'https':
            url = t['public_url']
            break
    if not url and data.get('tunnels'):
        url = data['tunnels'][0]['public_url']
    if url:
        print('=' * 64)
        print('  NGROK TUNNEL ACTIVE')
        print('=' * 64)
        print()
        print('  Public URL  : ' + url)
        print('  Dashboard   : ' + url + '/')
        print('  US30 JSON   : ' + url + '/api/live-state/US30')
        print()
        print('  PINE SCRIPT SETUP:')
        print('  Settings -> Data Source -> Dashboard URL (ngrok)')
        print('  Paste: ' + url)
        print()
        print('=' * 64)
    else:
        print('  [WARN] Could not detect ngrok URL yet.')
        print('  Check the ngrok window for the https:// URL.')
except Exception as e:
    print('  [WARN] ngrok API not ready: ' + str(e))
    print('  Check the ngrok window for the https:// URL.')
"

echo.
echo ============================================================
echo   NEXT STEP: Start the monitor
echo ============================================================
echo.
echo   Open a new terminal and run ONE of:
echo.
echo   Dry run (no webhooks):
echo     py -3 live\monitor.py --tier1
echo.
echo   Live mode (real webhooks + WhatsApp):
echo     py -3 live\monitor.py --tier1 --live
echo.
echo   Continuous loop (recommended):
echo     py -3 live\monitor.py --live
echo.
echo   Phone dashboard: open your phone browser and go to
echo   the ngrok URL shown above (same WiFi not required)
echo.
echo ============================================================
echo.
pause
