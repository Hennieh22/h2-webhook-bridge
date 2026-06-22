# H2 Auto Brief — Task Scheduler Setup
# Run this once as Administrator (right-click PowerShell → Run as Administrator)
# Creates 6 daily tasks for auto_trade.bat at SAST times

$bat   = "C:\Users\Admin\Desktop\H2_QUANT_V1\cowork\auto_trade.bat"
$user  = $env:USERNAME
$times = @("02:00", "06:00", "10:00", "14:00", "18:00", "22:00")

foreach ($t in $times) {
    $name = "H2_AutoBrief_$($t -replace ':','')"
    schtasks /delete /tn $name /f 2>$null | Out-Null
    $r = schtasks /create /tn $name /tr "cmd /c `"$bat`"" /sc DAILY /st $t /ru $user /rl HIGHEST /f
    Write-Host "$name : $r"
}

# ── Backup tasks (11:00 and 20:00 daily) ────────────────────────────────────
$backup_bat = "C:\Users\Admin\Desktop\H2_QUANT_V1\cowork\backup.bat"
foreach ($entry in @(@{tn="H2 Backup Morning"; st="11:00"}, @{tn="H2 Backup Evening"; st="20:00"})) {
    schtasks /delete /tn $entry.tn /f 2>$null | Out-Null
    $r = schtasks /create /tn $entry.tn /tr "cmd /c `"$backup_bat`"" /sc DAILY /st $entry.st /ru $user /rl HIGHEST /f
    Write-Host "$($entry.tn) : $r"
}

Write-Host ""
Write-Host "Verify briefs: schtasks /query /fo TABLE /tn `"H2_AutoBrief*`""
Write-Host "Verify backup: schtasks /query /fo TABLE /tn `"H2 Backup*`""
