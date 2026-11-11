<#
.SYNOPSIS
  删除 register-task.ps1 注册的定时任务。
#>
param([string]$TaskName = "FarerecordCollect")
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "已删除任务 $TaskName 。"
} else {
    Write-Host "没有找到任务 $TaskName 。"
}
