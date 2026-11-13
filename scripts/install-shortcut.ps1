<#
.SYNOPSIS
  在桌面和开始菜单创建"航价记"快捷方式，双击直接打开桌面窗口（没有黑色命令行窗口）。
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\install-shortcut.ps1
  powershell -ExecutionPolicy Bypass -File scripts\install-shortcut.ps1 -Remove
#>
param([switch]$Remove)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"
$icon = Join-Path $root "web\icon.ico"
$targets = @(
    (Join-Path ([Environment]::GetFolderPath("Desktop")) "航价记.lnk"),
    (Join-Path ([Environment]::GetFolderPath("Programs")) "航价记.lnk")
)

if ($Remove) {
    foreach ($lnk in $targets) {
        if (Test-Path $lnk) { Remove-Item $lnk; Write-Host "已删除 $lnk" }
    }
    return
}
if (-not (Test-Path $pythonw)) {
    throw "找不到 $pythonw ，请先按 README 完成安装。"
}
$shell = New-Object -ComObject WScript.Shell
foreach ($lnk in $targets) {
    $s = $shell.CreateShortcut($lnk)
    $s.TargetPath = $pythonw
    $s.Arguments = "-m farerecord.desktop"
    $s.WorkingDirectory = $root
    $s.Description = "航价记：机票价格记录与提醒"
    if (Test-Path $icon) { $s.IconLocation = $icon }
    $s.Save()
    Write-Host "已创建 $lnk"
}
Write-Host "双击桌面上的“航价记”即可打开。"
