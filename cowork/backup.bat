@echo off
:: H2 Quant System — Backup Script
:: Copies project to C:\Users\Admin\H2_Backups\H2_Backup_<timestamp>\
:: Creates a git bundle, copies key outputs, writes BACKUP_MANIFEST.json
:: Run manually or via Task Scheduler (11:00 and 20:00 daily)

set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
cd /d C:\Users\Admin\Desktop\H2_QUANT_V1

echo [%DATE% %TIME%] H2 Backup started
python cowork\backup_runner.py
echo [%DATE% %TIME%] H2 Backup complete
