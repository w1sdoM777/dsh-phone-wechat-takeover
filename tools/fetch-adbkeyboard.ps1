# 下载 ADBKeyboard.apk（中文输入用；可选）
# 来源：https://github.com/senzhk/ADBKeyboard （Apache-2.0）
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$apk = Join-Path $root "phone\ADBKeyboard.apk"

if (Test-Path $apk) {
    Write-Host "ADBKeyboard.apk 已存在：$apk"
    exit 0
}

$urls = @(
    "https://raw.githubusercontent.com/senzhk/ADBKeyboard/master/ADBKeyboard.apk",
    "https://github.com/senzhk/ADBKeyboard/raw/master/ADBKeyboard.apk"
)
$ok = $false
foreach ($u in $urls) {
    try {
        Write-Host "下载 $u"
        Invoke-WebRequest -Uri $u -OutFile $apk -UseBasicParsing -TimeoutSec 120
        if ((Get-Item $apk).Length -gt 100KB) { $ok = $true; break }
    } catch {
        Write-Host "失败：$($_.Exception.Message)"
    }
}
if (-not $ok) {
    Remove-Item $apk -ErrorAction SilentlyContinue
    throw "下载失败：请手动从 https://github.com/senzhk/ADBKeyboard 下载 ADBKeyboard.apk 放到 phone\ 目录"
}

Write-Host "已下载：$apk"
Write-Host ""
Write-Host "安装到手机（可选，仅中文回复需要）："
Write-Host "  adb install -r `"$apk`""
Write-Host "  adb shell ime enable com.android.adbkeyboard/.AdbIME"
Write-Host "（cn_input.py 会在发送时临时切换输入法并在结束后恢复原输入法）"
