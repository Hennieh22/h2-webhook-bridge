@echo off
:: ============================================================
:: H2 Quant v1 — Full System Startup
:: Double-click to launch all 5 components.
::
:: Windows 1: Live Monitor      (live/monitor.py)
:: Windows 2: Dashboard Server  (dashboard/app.py)
:: Windows 3: ngrok Tunnel      (ngrok http 5050)
:: Windows 4: TV Bridge         (pine/tv_bridge.py)
:: Windows 5: Session Brief     (cowork/H2_brief_generator.py)
::
:: Stop everything: double-click H2_STOP.bat
:: ============================================================

title H2 GO — Startup

set ROOT=C:\Users\Admin\Desktop\H2_QUANT_V1

echo.
echo  ============================================================
echo   H2 QUANT v1 — SYSTEM STARTING
echo  ============================================================
echo.

:: ── Window 1: Live Monitor ───────────────────────────────────
echo  [1/5] Starting Live Monitor...
start "H2 Monitor" powershell.exe -NoExit -ExecutionPolicy Bypass -Command ^
  "& { $host.UI.RawUI.WindowTitle = 'H2 Monitor'; Set-Location '%ROOT%'; Write-Host '--- H2 LIVE MONITOR ---' -ForegroundColor Cyan; python live/monitor.py }"

timeout /t 5 /nobreak > nul

:: ── Window 2: Dashboard Server ───────────────────────────────
echo  [2/5] Starting Dashboard Server on port 5050...
start "H2 Dashboard" powershell.exe -NoExit -ExecutionPolicy Bypass -Command ^
  "& { $host.UI.RawUI.WindowTitle = 'H2 Dashboard'; Set-Location '%ROOT%'; Write-Host '--- H2 DASHBOARD  http://localhost:5050 ---' -ForegroundColor Green; python dashboard/app.py }"

timeout /t 3 /nobreak > nul

:: ── Window 3: ngrok Tunnel ────────────────────────────────────
echo  [3/5] Starting ngrok tunnel on port 5050...
start "H2 Tunnel" powershell -NoExit -Command "cd 'C:\Users\Admin\Desktop\H2_QUANT_V1'; .\ngrok.exe http 5050"

timeout /t 5 /nobreak > nul

:: ── Window 4: TV Bridge ───────────────────────────────────────
echo  [4/5] Starting TV Bridge (5-min loop)...
start "H2 TV Bridge" powershell.exe -NoExit -ExecutionPolicy Bypass -Command ^
  "& { $host.UI.RawUI.WindowTitle = 'H2 TV Bridge'; Set-Location '%ROOT%'; Write-Host '--- H2 TV BRIDGE ---' -ForegroundColor Magenta; python pine/tv_bridge.py --loop }"

timeout /t 10 /nobreak > nul

:: ── Window 5: Session Brief ───────────────────────────────────
echo  [5/5] Generating session brief...
start "H2 Brief" powershell.exe -NoExit -ExecutionPolicy Bypass -Command ^
  "& { $host.UI.RawUI.WindowTitle = 'H2 Brief'; Set-Location '%ROOT%'; Write-Host '--- H2 SESSION BRIEF ---' -ForegroundColor Gold; python cowork/H2_brief_generator.py; Write-Host '' -ForegroundColor White; Write-Host 'Brief generated.' -ForegroundColor Green; Write-Host 'File: outputs/H2_daily_briefing.md' -ForegroundColor Cyan; Write-Host ''; Write-Host 'Press Enter to close this window...'; Read-Host }"

:: ── Print final status ────────────────────────────────────────
echo.
echo  ============================================================
echo   H2 SYSTEM RUNNING
echo  ============================================================
echo.
echo   Window 1 - H2 Monitor   : live/monitor.py (5-min cycle)
echo   Window 2 - H2 Dashboard : http://localhost:5050
echo   Window 3 - H2 Tunnel    : check for ngrok https:// URL
echo   Window 4 - H2 TV Bridge : pine/tv_bridge.py --loop
echo   Window 5 - H2 Brief     : outputs/H2_daily_briefing.md
echo.
echo   PINE SCRIPT SETUP:
echo   Check the H2 Tunnel window for the ngrok https:// URL.
echo   TradingView: Settings ^> Data Source ^> Dashboard URL ^(ngrok^)
echo   Paste the https://... URL from the tunnel window.
echo.
echo   LOCAL DASHBOARD (phone on same WiFi):
echo   http://192.168.101.245:5050
echo.
echo   To stop everything: double-click H2_STOP.bat
echo.
echo  ============================================================
echo.
pause
