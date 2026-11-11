<#
.SYNOPSIS
  注册 Windows 任务计划：每天定时运行 farerecord collect（默认 4 次）。
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\register-task.ps1
  powershell -ExecutionPolicy Bypass -File scripts\register-task.ps1 -Times 08:00,20:00
#>
param(
    [string[]]$Times = @("00:00", "06:00", "12:00", "18:00"),
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

$command = "& '$exe' collect *>> '$log'"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -Command `"$command`"" `
    -WorkingDirectory $root
$triggers = foreach ($t in $Times) { New-ScheduledTaskTrigger -Daily -At $t }
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers -Settings $settings `
    -Description "航价记：定时抓取携程机票价格" -Force | Out-Null

Write-Host "已注册任务 $TaskName ，每天 $($Times -join '、') 运行。"
Write-Host "日志：$log"
Write-Host "错过的时间点（例如电脑关机）会在开机后补跑一次。"
