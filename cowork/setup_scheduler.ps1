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

Write-Host ""
Write-Host "Verify with:  schtasks /query /fo LIST /tn H2_AutoBrief*"
