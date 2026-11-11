<#
.SYNOPSIS
  注册 Windows 任务计划：定时运行 farerecord collect（默认每 60 分钟一次）。
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\register-task.ps1
  powershell -ExecutionPolicy Bypass -File scripts\register-task.ps1 -IntervalMinutes 120
#>
param(
    [ValidateRange(30, 1440)][int]$IntervalMinutes = 60,
    [string]$TaskName = "FarerecordCollect"
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$exe = Join-Path $root ".venv\Scripts\farerecord.exe"
if (-not (Test-Path $exe)) {
    throw "找不到 $exe ，请先按 README 完成安装。"
}
$logDir = Join-Path $root "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir "collect.log"

# --if-due：桌面端开着时它自己也会定时抓取，谁先到点谁抓，另一个跳过
$command = "& '$exe' collect --if-due *>> '$log'"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -Command `"$command`"" `
    -WorkingDirectory $root
$start = (Get-Date).Date.AddHours((Get-Date).Hour + 1)
$trigger = New-ScheduledTaskTrigger -Once -At $start `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "航价记：定时抓取携程机票价格" -Force | Out-Null

Write-Host "已注册任务 $TaskName ，从 $($start.ToString('HH:mm')) 起每 $IntervalMinutes 分钟运行一次。"
Write-Host "日志：$log"
Write-Host "错过的时间点（例如电脑关机）会在开机后补跑一次。"
