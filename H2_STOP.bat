@echo off
:: ============================================================
:: H2 Quant v1 — Full System Stop
:: Closes all H2 PowerShell windows cleanly.
:: ============================================================

title H2 STOP

echo.
echo  ============================================================
echo   H2 QUANT v1 — STOPPING ALL SYSTEMS
echo  ============================================================
echo.

:: ── Kill by window title ─────────────────────────────────────
echo  Stopping H2 Monitor...
taskkill /FI "WINDOWTITLE eq H2 Monitor*" /F > nul 2>&1

echo  Stopping H2 Dashboard...
taskkill /FI "WINDOWTITLE eq H2 Dashboard*" /F > nul 2>&1

echo  Stopping H2 Tunnel (ngrok)...
taskkill /FI "WINDOWTITLE eq H2 Tunnel*" /F > nul 2>&1
taskkill /IM ngrok.exe /F > nul 2>&1

echo  Stopping H2 TV Bridge...
taskkill /FI "WINDOWTITLE eq H2 TV Bridge*" /F > nul 2>&1

echo  Stopping H2 Brief...
taskkill /FI "WINDOWTITLE eq H2 Brief*" /F > nul 2>&1

:: ── Kill any orphan python processes matching H2 scripts ─────
echo  Cleaning up any remaining Python processes...
wmic process where "name='python.exe' and CommandLine like '%%monitor.py%%'"   delete > nul 2>&1
wmic process where "name='python.exe' and CommandLine like '%%app.py%%'"        delete > nul 2>&1
wmic process where "name='python.exe' and CommandLine like '%%tv_bridge.py%%'"  delete > nul 2>&1
wmic process where "name='python.exe' and CommandLine like '%%H2_brief%%'"      delete > nul 2>&1
wmic process where "name='python3.exe' and CommandLine like '%%monitor.py%%'"   delete > nul 2>&1
wmic process where "name='python3.exe' and CommandLine like '%%app.py%%'"       delete > nul 2>&1
wmic process where "name='python3.exe' and CommandLine like '%%tv_bridge.py%%'" delete > nul 2>&1

echo.
echo  ============================================================
echo   H2 SYSTEM STOPPED
echo  ============================================================
echo.
echo   All H2 windows closed.
echo   Live state: outputs/H2_live_state.json preserved.
echo   Signal log: outputs/H2_signal_log.csv preserved.
echo.
echo   To restart: double-click H2_GO.bat
echo.
echo  ============================================================
echo.
pause
