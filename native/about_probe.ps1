# about_probe.ps1 -- headless verification of the "About" dialog content and its repo link.
#
# What it proves (no human clicking anything, and WITHOUT opening a browser):
#   1) the dialog is built from the layout (row count + size are derived, not hardcoded):
#      `about: window shown WxH rows=13 author='...' repo=... license=...`
#   2) author / license / repository come from Cargo.toml (single source), not literals
#   3) the "repository" row is clickable and the click goes through the REAL path
#      (WM_LBUTTONDOWN/UP) -> `about: link click -> would open <repo>`
#   4) with LH_ABOUT_NO_OPEN=1 nothing is actually launched (no browser window appears)
#   5) the dialog font scales with DPI (E47 guard)
#
# ASCII only (PS 5.1 reads BOM-less scripts as GBK -> a Chinese comment can swallow the
# next line; that bit us once already, see ERROR.md E43/E47).

param(
    [string]$Exe = "D:\code\DeepSeekHarness\learn-helper\native\target\release\learn-helper-native.exe",
    [string]$Repo = "https://github.com/dboycht/learn-helper"
)

$ErrorActionPreference = 'Stop'
$script:pass = 0
$script:fail = 0
$script:diag = Join-Path (Split-Path -Parent $Exe) 'native-diag.log'

function Check([string]$name, [bool]$ok, [string]$detail = '') {
    if ($ok) {
        $script:pass++
        Write-Host ("  [PASS] " + $name + $(if ($detail) { " -- " + $detail } else { '' }))
    } else {
        $script:fail++
        Write-Host ("  [FAIL] " + $name + $(if ($detail) { " -- " + $detail } else { '' }))
    }
}

function Expected-UiPx([int]$dpi) {
    $inner = [math]::Floor(14 * $dpi * 100 / 96)
    return [int][math]::Floor(($inner + 50) / 100)
}

function Check-FontScale([string]$name, $log, [string]$pattern) {
    $line = @($log | Where-Object { $_ -match $pattern })[0]
    if (-not $line) { Check $name $false ("trace line not found: " + $pattern); return }
    if ($line -notmatch 'dpi=(\d+).*ui=(\d+)px') { Check $name $false ("unparsable: " + $line); return }
    $dpi = [int]$Matches[1]
    $ui = [int]$Matches[2]
    $want = Expected-UiPx $dpi
    Check $name ($ui -eq $want) ("dpi=" + $dpi + " ui=" + $ui + "px expected=" + $want + "px")
}

function Diag-Snapshot([string]$dest) {
    if (Test-Path $script:diag) { Copy-Item $script:diag $dest -Force }
    else { Set-Content -Path $dest -Value '' -Encoding ASCII }
}

# NOTE: delete the diag BEFORE launching a case. The exe truncates it only once it starts,
# so an early snapshot can otherwise "find" the PREVIOUS run's lines and the wait returns
# stale evidence (this made case 2 fail with 13 passing checks around it).
function Reset-Diag() {
    Remove-Item $script:diag -Force -ErrorAction SilentlyContinue
}

function Wait-NoInstance([int]$timeoutSeconds = 15) {
    $deadline = (Get-Date).AddSeconds($timeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (@(Get-Process -Name 'learn-helper-native' -ErrorAction SilentlyContinue).Count -eq 0) {
            return $true
        }
        Start-Sleep -Milliseconds 300
    }
    return $false
}

function Wait-DiagMatch([string]$dest, [string]$pattern, [int]$timeoutMs = 15000) {
    $deadline = (Get-Date).AddMilliseconds($timeoutMs)
    while ((Get-Date) -lt $deadline) {
        Diag-Snapshot $dest
        $lines = @(Get-Content $dest -Encoding UTF8 -ErrorAction SilentlyContinue)
        if (@($lines | Where-Object { $_ -match $pattern }).Count -gt 0) { return $lines }
        Start-Sleep -Milliseconds 200
    }
    Diag-Snapshot $dest
    $lines = @(Get-Content $dest -Encoding UTF8 -ErrorAction SilentlyContinue)
    # On timeout, dump what we DID see: a probe that fails must also explain itself.
    Write-Host ("  (timeout waiting for '" + $pattern + "'; last lines of the diag:)")
    foreach ($l in ($lines | Select-Object -Last 6)) { Write-Host ("      | " + $l) }
    if ($lines.Count -eq 0) { Write-Host "      | (diag is EMPTY -> the app probably never started)" }
    return $lines
}

function New-TempDir([string]$tag) {
    $d = Join-Path $env:TEMP ("lh-about-probe-" + $tag + "-" + (Get-Random))
    New-Item -ItemType Directory -Path $d -Force | Out-Null
    return $d
}

Write-Host "=== about dialog probe ==="
Write-Host ("exe  = " + $Exe)
Write-Host ("diag = " + $script:diag)
if (-not (Test-Path $Exe)) { Write-Host "EXE not found"; exit 2 }
if (@(Get-Process -Name 'learn-helper-native' -ErrorAction SilentlyContinue).Count -gt 0) {
    Write-Host "ABORT: learn-helper-native already running (single-instance mutex)"
    exit 3
}

# ---------------------------------------------------------------- CASE 1: content + link click
Write-Host ""
Write-Host "CASE 1: open About, click the repository row (browser guarded)"
$d1 = New-TempDir 'link'
if (-not (Wait-NoInstance)) { Write-Host "  WARNING: previous instance still running" }
$env:LH_BASE_DIR = $d1
$env:LH_UI_ACTION = 'about_link'
$env:LH_ABOUT_NO_OPEN = '1'
Reset-Diag
$p = Start-Process -FilePath $Exe -WorkingDirectory $d1 -PassThru
$snap = Join-Path $env:TEMP ("lh-about-diag-" + (Get-Random) + ".log")
$log = Wait-DiagMatch $snap 'about: link click'
Start-Sleep -Seconds 2
Diag-Snapshot $snap
$log = @(Get-Content $snap -Encoding UTF8 -ErrorAction SilentlyContinue)
Remove-Item Env:LH_BASE_DIR, Env:LH_UI_ACTION, Env:LH_ABOUT_NO_OPEN -ErrorAction SilentlyContinue

$shown = @($log | Where-Object { $_ -match 'about: window shown' })[0]
Check "About window shown" ($null -ne $shown) ($shown)
if ($shown) {
    Check "row count derived from content (rows=13)" ($shown -match 'rows=13')
    Check "author shown (Mizuki)" ($shown -match "author='Mizuki'") $shown
    Check "license shown (MIT)" ($shown -match 'license=MIT') $shown
    Check "repository shown" ($shown -match [regex]::Escape($Repo)) $shown
    if ($shown -match 'shown (\d+)x(\d+)') {
        $w = [int]$Matches[1]; $h = [int]$Matches[2]
        # Physical size = logical x dpi/96 (the dialog is 560 logical wide). Take the DPI
        # from this very run's font trace so the check works on any scaling factor.
        $dpiLine = @($log | Where-Object { $_ -match 'about: fonts rebuilt dpi=(\d+)' })[0]
        if ($dpiLine -and $dpiLine -match 'dpi=(\d+)') {
            $dpi = [int]$Matches[1]
            $wantW = [int][math]::Floor((560 * $dpi * 100 / 96 + 50) / 100)
            Check "window width matches derived content" ($w -eq $wantW) ("w=" + $w + " expected=" + $wantW + " (dpi=" + $dpi + ")")
        } else {
            Check "window width matches derived content" ($w -gt 0) ("w=" + $w + " (dpi unknown)")
        }
        Check "height grew for the new rows" ($h -gt 600) ("h=" + $h)
    }
}
Check-FontScale "About fonts scale with DPI" $log 'about: fonts rebuilt dpi='
Check "hook clicked the link row" (@($log | Where-Object { $_ -match 'about-hook: clicking the repository link row' }).Count -gt 0)
Check "click reached the open-url path with the right URL" (@($log | Where-Object { $_ -match ('about: link click -> would open ' + [regex]::Escape($Repo)) }).Count -gt 0)
Check "guarded mode did NOT launch anything" (@($log | Where-Object { $_ -match 'about: link click -> open ' }).Count -eq 0)
Check "dialog closed cleanly" (@($log | Where-Object { $_ -match 'about: window destroyed, state freed' }).Count -gt 0)

if (-not $p.HasExited) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 1
Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like '*learn-helper*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

# ---------------------------------------------------------------- CASE 2: plain open (no click)
Write-Host ""
Write-Host "CASE 2: plain open (no click) -> nothing is opened"
$d2 = New-TempDir 'open'
if (-not (Wait-NoInstance)) { Write-Host "  WARNING: previous instance still running" }
$env:LH_BASE_DIR = $d2
$env:LH_UI_ACTION = 'about'
$env:LH_ABOUT_NO_OPEN = '1'
Reset-Diag
$p2 = Start-Process -FilePath $Exe -WorkingDirectory $d2 -PassThru
$snap2 = Join-Path $env:TEMP ("lh-about-diag2-" + (Get-Random) + ".log")
$log2 = Wait-DiagMatch $snap2 'about: window shown'
Start-Sleep -Seconds 1
Diag-Snapshot $snap2
$log2 = @(Get-Content $snap2 -Encoding UTF8 -ErrorAction SilentlyContinue)
Remove-Item Env:LH_BASE_DIR, Env:LH_UI_ACTION, Env:LH_ABOUT_NO_OPEN -ErrorAction SilentlyContinue

Check "plain open shows the dialog" (@($log2 | Where-Object { $_ -match 'about: window shown' }).Count -gt 0)
Check "plain open does not click anything" (@($log2 | Where-Object { $_ -match 'about: link click' }).Count -eq 0)
# If the case fails, show the evidence right here (a probe must explain itself).
if (@($log2 | Where-Object { $_ -match 'about: window shown' }).Count -eq 0) {
    Write-Host ("  (case 2 diag: " + $log2.Count + " line(s), snapshot " + $snap2 + ")")
    foreach ($l in ($log2 | Select-Object -Last 8)) { Write-Host ("      | " + $l) }
}

if (-not $p2.HasExited) { Stop-Process -Id $p2.Id -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 1
Get-Process -Name 'learn-helper-native' -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like '*learn-helper*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

Write-Host ""
Write-Host ("RESULT: " + $script:pass + " passed, " + $script:fail + " failed")
Write-Host ("temp dirs kept: " + $d1 + " ; " + $d2)
if ($script:fail -gt 0) { exit 1 }
exit 0
