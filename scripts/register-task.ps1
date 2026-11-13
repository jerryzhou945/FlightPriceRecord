<#
.SYNOPSIS
  注册 Windows 任务计划：每 15 分钟检查一次，到了设定的抓取间隔就运行 farerecord collect。
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\register-task.ps1
  powershell -ExecutionPolicy Bypass -File scripts\register-task.ps1 -IntervalMinutes 30
#>
param(
    [ValidateRange(15, 1440)][int]$IntervalMinutes = 15,
    [string]$TaskName = "FarerecordCollect"
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$exe = Join-Path $root ".venv\Scripts\pythonw.exe"   # pythonw：后台运行，不会每次闪一下黑窗口
if (-not (Test-Path $exe)) {
    throw "找不到 $exe ，请先按 README 完成安装。"
}
$logDir = Join-Path $root "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir "collect.log"

# --if-due：到了设定的抓取间隔才真的抓。桌面端开着时它自己也会抓，谁先到点谁抓，另一个跳过
$action = New-ScheduledTaskAction -Execute $exe `
    -Argument "-m farerecord.cli --log-file `"$log`" collect --if-due" `
    -WorkingDirectory $root
$start = (Get-Date).AddMinutes(1)
$trigger = New-ScheduledTaskTrigger -Once -At $start `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "航价记：定时抓取携程机票价格" -Force | Out-Null

Write-Host "已注册任务 $TaskName ，每 $IntervalMinutes 分钟检查一次；实际多久抓一次由界面“抓取”页（或 config.yaml 的 collector.interval_minutes）决定。"
Write-Host "日志：$log"
Write-Host "错过的时间点（例如电脑关机）会在开机后补跑一次。"
