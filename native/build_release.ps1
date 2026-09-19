# build_release.ps1 -- build the full 2.x distribution (ASCII only, per rules/01 section 8.2:
# PowerShell 5.1 reads BOM-less files as GBK, so Chinese here would break parsing).
#
#   dist\learn-helper-<ver>\
#     LearnHelper.exe            <- native Rust UI, ~0.3 MB, zero dependency
#     backend\learn-helper-core.exe <- PyInstaller onefile backend (Playwright inside)
#     Run.bat                    <- plain ASCII launcher
#     README.txt
#
# Then:  Compress-Archive dist\learn-helper-<ver> dist\learn-helper-<ver>.zip
#
# Usage:  powershell -ExecutionPolicy Bypass -File native\build_release.ps1

param(
    [switch]$SkipBackend,
    [switch]$SkipUi,
    [switch]$Zip
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$version = $null

# Version single source: Cargo.toml (native) -- keep config.py / csproj in step at release.
$cargo = Join-Path $PSScriptRoot 'Cargo.toml'
foreach ($line in Get-Content $cargo) {
    if ($line -match '^\s*version\s*=\s*"([^"]+)"') { $version = $Matches[1]; break }
}
if (-not $version) { throw "cannot read version from $cargo" }

$dist = Join-Path $root "dist\learn-helper-$version"
Write-Host "=== learn-helper $version build ===" -ForegroundColor Cyan

# ---------------------------------------------------------------- UI (Rust)
if (-not $SkipUi) {
    Write-Host "[1/3] cargo build --release (native UI)" -ForegroundColor Yellow
    Push-Location $PSScriptRoot
    & cargo build --release
    if ($LASTEXITCODE -ne 0) { Pop-Location; throw "cargo build failed" }
    Pop-Location
}

$uiExe = Join-Path $PSScriptRoot 'target\release\learn-helper-native.exe'
if (-not (Test-Path $uiExe)) { throw "native exe not found: $uiExe (run without -SkipUi)" }

# ---------------------------------------------------------------- icon
# Stamp the multi-size icon into the freshly built exe. `cargo build` always produces an exe without
# icon resources, so this MUST run after every build (a rebuild silently drops the icon otherwise).
# The ICO itself is generated from native\logo-src.png by _tools\make-icon.ps1 (16/24/32/48/64/128/256).
$icon = Join-Path $PSScriptRoot 'logo.ico'
if (Test-Path $icon) {
    Write-Host "[icon] stamping logo.ico into the exe" -ForegroundColor Yellow
    & powershell -ExecutionPolicy Bypass -File (Join-Path $root '_tools\make-icon.ps1') `
        -IconFile $icon -ExeFile $uiExe
    if ($LASTEXITCODE -ne 0) { throw "icon stamping failed" }
} else {
    Write-Host "[icon] SKIPPED: $icon not found" -ForegroundColor DarkYellow
}

# ---------------------------------------------------------------- backend
$backendExe = Join-Path $root '_release\pyi\learn-helper-core.exe'
if (-not $SkipBackend) {
    Write-Host "[2/3] PyInstaller onefile (Python backend)" -ForegroundColor Yellow
    Push-Location $root
    & py -3.10 -m PyInstaller --noconfirm --clean --onefile `
        --name learn-helper-core `
        --distpath '_release\pyi' `
        --workpath '_release\pyi-work' `
        --specpath '_release\pyi-work' `
        'backend\main.py'
    $pyiCode = $LASTEXITCODE
    Pop-Location
    if ($pyiCode -ne 0 -or -not (Test-Path $backendExe)) { throw "PyInstaller build failed" }
}
if (-not (Test-Path $backendExe)) { throw "backend exe not found: $backendExe" }

# ---------------------------------------------------------------- stage
Write-Host "[3/3] staging -> $dist" -ForegroundColor Yellow
if (Test-Path $dist) { Remove-Item -Recurse -Force $dist }
New-Item -ItemType Directory -Force -Path $dist | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $dist 'backend') | Out-Null

Copy-Item $uiExe (Join-Path $dist 'LearnHelper.exe') -Force
Copy-Item $backendExe (Join-Path $dist 'backend\learn-helper-core.exe') -Force

# Launcher: plain ASCII so it works on any code page. The UI finds the backend
# next to itself (backend\learn-helper-core.exe) and starts it automatically.
$runBat = @"
@echo off
rem learn-helper $version -- just run LearnHelper.exe.
rem The Python backend (backend\learn-helper-core.exe) is started automatically
rem by the UI; nothing needs to be installed on this machine.
start "" "%~dp0LearnHelper.exe"
"@
Set-Content -Path (Join-Path $dist 'Run.bat') -Value $runBat -Encoding ASCII

$config = @"
{
  "server_url": "http://127.0.0.1:8000",
  "answer": {
    "mode": "server",
    "workers": 4,
    "solver_timeout": 240,
    "retry": 2
  },
  "run": {
    "video_speed": 2.0,
    "auto_submit": true,
    "auto_launch_browser": true,
    "skip_quiz_only": false
  }
}
"@
Set-Content -Path (Join-Path $dist 'config.example.json') -Value $config -Encoding ASCII

$readme = @"
learn-helper $version
=====================

How to run
----------
1. Double-click LearnHelper.exe (or Run.bat).
2. On first launch a sandbox browser profile is created next to the exe
   (browser_profile\) and Edge is started with remote debugging on port 9222.
3. Open your course page in that browser, then click
   "check/refresh pages" in the app and pick the page, then "start".

No runtime needs to be installed: this package is fully self-contained.

Files
-----
  LearnHelper.exe            UI (native Win32, ~0.3 MB, zero dependency)
  backend\learn-helper-core.exe   Python backend (Playwright automation + solving)
  config.example.json        copy to config.json and edit to change the
                             answering backend / model / speed
  logs\learn_helper.log      created on first run (backend log)
  native-diag.log            created on first run (UI diagnostics)

Settings are read from config.json next to LearnHelper.exe.
"@
Set-Content -Path (Join-Path $dist 'README.txt') -Value $readme -Encoding ASCII

# ---------------------------------------------------------------- summary
$uiSize = (Get-Item (Join-Path $dist 'LearnHelper.exe')).Length / 1MB
$beSize = (Get-Item (Join-Path $dist 'backend\learn-helper-core.exe')).Length / 1MB
$total = (Get-ChildItem $dist -Recurse -File | Measure-Object Length -Sum).Sum / 1MB
Write-Host ""
Write-Host ("  UI      : {0:N2} MB" -f $uiSize)
Write-Host ("  backend : {0:N1} MB" -f $beSize)
Write-Host ("  TOTAL   : {0:N1} MB" -f $total) -ForegroundColor Green
Write-Host "  staged  : $dist"

if ($Zip) {
    # NOTE: do not name this $zip -- PowerShell is case-insensitive and it would
    # collide with the -Zip switch parameter.
    $zipPath = Join-Path $root "dist\learn-helper-$version.zip"
    if (Test-Path $zipPath) { Remove-Item $zipPath -Force }
    Compress-Archive -Path (Join-Path $dist '*') -DestinationPath $zipPath
    Write-Host ("  zip     : {0} ({1:N1} MB)" -f $zipPath, ((Get-Item $zipPath).Length / 1MB)) -ForegroundColor Green
}
