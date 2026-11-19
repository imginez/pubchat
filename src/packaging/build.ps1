# PubChat Windows 打包
#
# powershell -ExecutionPolicy Bypass -File packaging\build.ps1
# powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -BundleCloudflared
#
# 产物：dist\PubChat.exe（单文件）。结束后打印体积与 SHA256。
param(
    [switch]$BundleCloudflared,
    [string]$Cloudflared = ""
)

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root

Write-Host "== 检查 Python =="
python -c "import sys, PyInstaller, websockets; print(sys.version.split()[0], 'pyinstaller', PyInstaller.__version__, 'websockets', websockets.__version__)"

if ($BundleCloudflared) {
    if (-not $Cloudflared) {
        $candidates = @(
            "$env:ProgramFiles\cloudflared\cloudflared.exe",
            "${env:ProgramFiles(x86)}\cloudflared\cloudflared.exe",
            "$env:LOCALAPPDATA\cloudflared\cloudflared.exe"
        )
        $Cloudflared = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
    }
    if (-not $Cloudflared -or -not (Test-Path $Cloudflared)) {
        throw "没找到 cloudflared.exe，请用 -Cloudflared <路径> 指定"
    }
    $env:PUBCHAT_BUNDLE_CLOUDFLARED = "1"
    $env:PUBCHAT_CLOUDFLARED = (Resolve-Path $Cloudflared).Path
    Write-Host "== 打包 cloudflared: $env:PUBCHAT_CLOUDFLARED =="
} else {
    Remove-Item Env:PUBCHAT_BUNDLE_CLOUDFLARED -ErrorAction SilentlyContinue
    Remove-Item Env:PUBCHAT_CLOUDFLARED -ErrorAction SilentlyContinue
}

Write-Host "== PyInstaller =="
python -m PyInstaller --noconfirm --clean packaging\pubchat.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 失败" }

$exe = Join-Path $root "dist\PubChat.exe"
if (-not (Test-Path $exe)) { throw "没有产出 dist\PubChat.exe" }

$size = [math]::Round((Get-Item $exe).Length / 1MB, 1)
$hash = (Get-FileHash $exe -Algorithm SHA256).Hash.ToLower()
Write-Host ""
Write-Host "== 完成 =="
Write-Host "文件: $exe"
Write-Host "体积: $size MB"
Write-Host "SHA256: $hash"
