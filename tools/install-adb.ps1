# 下载/修复 platform-tools（可重复执行）
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$tools = Join-Path $root "tools"
$zip = Join-Path $tools "platform-tools.zip"
$adb = Join-Path $tools "platform-tools\adb.exe"

New-Item -ItemType Directory -Force $tools | Out-Null

if (Test-Path $adb) {
    & $adb version
    Write-Host "adb 已存在，跳过下载"
    exit 0
}

$urls = @(
    "https://dl.google.com/android/repository/platform-tools-latest-windows.zip",
    "https://googledownloads.cn/android/repository/platform-tools-latest-windows.zip"
)
$ok = $false
foreach ($u in $urls) {
    try {
        Write-Host "下载 $u"
        Invoke-WebRequest -Uri $u -OutFile $zip -UseBasicParsing -TimeoutSec 120
        if ((Get-Item $zip).Length -gt 1MB) { $ok = $true; break }
    } catch {
        Write-Host "失败: $($_.Exception.Message)"
    }
}
if (-not $ok) { throw "下载失败：手动下载 platform-tools 解压到 tools\ 目录" }

Expand-Archive -Path $zip -DestinationPath $tools -Force
& $adb version
Write-Host "完成"
