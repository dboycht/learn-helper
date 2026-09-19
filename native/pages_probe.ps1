# pages_probe.ps1 -- headless verification of the new "current page" dropdown picker.
#
# What it proves (no human clicking anything):
#   1) clicking the page box really opens the self-drawn dropdown
#      (`pagepicker: shown items=N ...` in native-diag.log)
#   2) picking an item goes all the way through: UI click -> POST /api/control select_page
#      -> backend hub -> **written to config.json: last_page_title** (the strongest proof)
#   3) the selection is REMEMBERED: a later start restores it
#      (`pagepicker: shown ... selected='<the one picked before>'`)
#   4) Esc closes the dropdown and changes nothing
#
# Isolation: LH_BASE_DIR points at a temp folder, so the real learn-helper\config.json
# is never touched. `LH_PROBE_PAGES` injects sample page titles (no browser needed);
# those titles are read from a UTF-8 data file so this script stays pure ASCII while the
# round-trip still exercises Chinese text (env -> UI -> HTTP -> config.json UTF-8).
#
# ASCII only (PS 5.1 reads BOM-less scripts as GBK).

param(
    [string]$Exe = "D:\code\DeepSeekHarness\learn-helper\native\target\release\learn-helper-native.exe",
    [string]$SampleFile = "D:\code\DeepSeekHarness\learn-helper\native\pages_probe_sample.txt"
)

$ErrorActionPreference = 'Stop'
$script:pass = 0
$script:fail = 0
$script:diag = Join-Path (Split-Path -Parent $Exe) 'native-diag.log'

# Sample page titles, in UTF-8, from a data file (keeps this script ASCII-only).
$sampleLines = @([IO.File]::ReadAllLines($SampleFile, [Text.Encoding]::UTF8) | Where-Object { $_.Trim() -ne '' })
if ($sampleLines.Count -lt 2) { Write-Host ("SAMPLE FILE TOO SHORT: " + $SampleFile); exit 2 }
$SamplePages = ($sampleLines | ForEach-Object { $_.Trim() }) -join '|'

function Check([string]$name, [bool]$ok, [string]$detail = '') {
    if ($ok) {
        $script:pass++
        Write-Host ("  [PASS] " + $name + $(if ($detail) { " -- " + $detail } else { '' }))
    } else {
        $script:fail++
        Write-Host ("  [FAIL] " + $name + $(if ($detail) { " -- " + $detail } else { '' }))
    }
}

# Expected dropdown font pixel size for a given DPI, mirroring ui::dialog_font_sizes()
# (E47: the dropdown used to keep 96-DPI text while its rows grew 1.5x on a 150% screen).
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

function New-TempDir([string]$tag) {
    $d = Join-Path $env:TEMP ("lh-pages-probe-" + $tag + "-" + (Get-Random))
    New-Item -ItemType Directory -Path $d -Force | Out-Null
    return $d
}

function Read-Cfg([string]$dir) {
    $p = Join-Path $dir 'config.json'
    if (-not (Test-Path $p)) { return $null }
    return (Get-Content $p -Raw -Encoding UTF8 | ConvertFrom-Json)
}

function Diag-Snapshot([string]$dest) {
    if (Test-Path $script:diag) { Copy-Item $script:diag $dest -Force }
    else { Set-Content -Path $dest -Value '' -Encoding ASCII }
}

# NOTE: delete the diag BEFORE launching a case: the exe truncates it only once it starts,
# so an early snapshot can otherwise "find" the PREVIOUS run's lines (stale evidence).
function Reset-Diag() {
    Remove-Item $script:diag -Force -ErrorAction SilentlyContinue
}

# Wait until a trace line matching $pattern shows up, then return that run's lines.
# Why: "popup window exists" (polled via FindWindow) can be true a few milliseconds BEFORE
# the matching trace line is written -> snapshotting at that instant makes the check flaky.
# Always assert on evidence that has ARRIVED, not on evidence sampled at one moment.
function Wait-DiagMatch([string]$dest, [string]$pattern, [int]$timeoutMs = 4000) {
    $deadline = (Get-Date).AddMilliseconds($timeoutMs)
    while ((Get-Date) -lt $deadline) {
        Diag-Snapshot $dest
        $lines = @(Get-Content $dest -Encoding UTF8 -ErrorAction SilentlyContinue)
        if (@($lines | Where-Object { $_ -match $pattern }).Count -gt 0) { return $lines }
        Start-Sleep -Milliseconds 200
    }
    Diag-Snapshot $dest
    return @(Get-Content $dest -Encoding UTF8 -ErrorAction SilentlyContinue)
}

# Runs one UI case and returns that case's diag lines (the exe truncates the log on start).
# NOTE: the app is single-instance (named mutex). If the previous case's process is still
# shutting down, the new process exits(0) immediately and EVERY assertion in the case fails
# -> a confusing "everything is broken" run. So always wait for it to disappear first.
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

function Invoke-Ui([string]$dir, [string]$action, [int]$waitSeconds) {
    $snap = Join-Path $env:TEMP ("lh-pages-diag-" + (Get-Random) + ".log")
    if (-not (Wait-NoInstance)) {
        Write-Host "  WARNING: a previous learn-helper-native is still running; case may be bogus"
    }
    $env:LH_BASE_DIR = $dir
    # Guard: this probe does not test the auto-launch-browser feature, and the app arms that
    # feature 4 seconds after start. Several probes run longer than that, so without this the
    # app could really open the user's sandbox browser in the middle of a test (found by audit).
    $env:LH_NO_AUTO_BROWSER = '1'
    $env:LH_UI_ACTION = $action
    $env:LH_PROBE_PAGES = $SamplePages
    Reset-Diag
    $p = Start-Process -FilePath $Exe -WorkingDirectory $dir -PassThru
    $deadline = (Get-Date).AddSeconds($waitSeconds)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 400
        if ($p.HasExited) { break }
    }
    if (-not $p.HasExited) {
        Write-Host ("  (case still running after " + $waitSeconds + "s -> force-kill ui pid " + $p.Id + ")")
        Diag-Snapshot $snap
        Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
        Start-Sleep -Milliseconds 1500
    } else {
        Diag-Snapshot $snap
    }
    Remove-Item Env:LH_BASE_DIR -ErrorAction SilentlyContinue
    Remove-Item Env:LH_UI_ACTION -ErrorAction SilentlyContinue
    Remove-Item Env:LH_PROBE_PAGES -ErrorAction SilentlyContinue
    if (-not (Test-Path $snap)) { return @() }
    return @(Get-Content $snap -Encoding UTF8 -ErrorAction SilentlyContinue)
}

Write-Host "=== current-page dropdown probe ==="
Write-Host ("exe  = " + $Exe)
Write-Host ("diag = " + $script:diag)
if (-not (Test-Path $Exe)) { Write-Host "EXE not found"; exit 2 }

$running = @(Get-Process -Name 'learn-helper-native' -ErrorAction SilentlyContinue)
if ($running.Count -gt 0) {
    Write-Host "ABORT: learn-helper-native already running (single-instance mutex)"
    exit 3
}

$item0 = ($SamplePages -split '\|')[0]
$item1 = ($SamplePages -split '\|')[1]

# ---------------------------------------------------------------- CASE 1: pick item #2
Write-Host ""
Write-Host "CASE 1: open dropdown + pick 2nd item (should reach the backend and disk)"
$d1 = New-TempDir 'pick'
$log1 = Invoke-Ui $d1 'pages_select' 40
$cfg1 = Read-Cfg $d1

Check "UI injected sample pages" (@($log1 | Where-Object { $_ -match 'LH_PROBE_PAGES injected 3 item' }).Count -gt 0)
Check "dropdown opened (items=3)" (@($log1 | Where-Object { $_ -match 'pagepicker: shown items=3' }).Count -gt 0)
# E47: the dropdown font must scale with DPI. Keep this script pure ASCII (see the note
# in settings_probe.ps1: a Chinese comment can swallow the next line under PS 5.1/GBK).
Check-FontScale "dropdown fonts scale with DPI" $log1 'pagepicker: fonts rebuilt dpi='
Check "hook auto-picked index #1" (@($log1 | Where-Object { $_ -match 'pagepicker: auto-pick #1' }).Count -gt 0)
Check "item click handled" (@($log1 | Where-Object { $_ -match 'pagepicker: picked #1' }).Count -gt 0)
Check "dropdown closed itself after picking" (@($log1 | Where-Object { $_ -match 'pagepicker: window destroyed' }).Count -gt 0)

if ($null -ne $cfg1) {
    Check "selection persisted to config.json: last_page_title" ($cfg1.last_page_title -eq $item1) ("got '" + $cfg1.last_page_title + "'")
} else {
    Check "config.json written by the backend" $false
}

# ---------------------------------------------------------------- CASE 2: selection remembered
Write-Host ""
Write-Host "CASE 2: restart -> the remembered selection is restored"
$log2 = Invoke-Ui $d1 'pages' 30
$restored = @($log2 | Where-Object { $_ -match "pagepicker: shown" -and $_ -match [regex]::Escape($item1) }).Count -gt 0
Check "restored selection passed into the dropdown" $restored
Check "dropdown opened in case 2" (@($log2 | Where-Object { $_ -match 'pagepicker: shown items=3' }).Count -gt 0)
$cfg2 = Read-Cfg $d1
if ($null -ne $cfg2) {
    Check "config unchanged by merely opening the dropdown" ($cfg2.last_page_title -eq $item1)
}

# ---------------------------------------------------------------- CASE 3: empty list + Esc
Write-Host ""
Write-Host "CASE 3: empty page list -> dropdown shows a hint, then Esc closes"

# Esc via a real key message to the popup window
Add-Type -TypeDefinition @'
using System; using System.Text; using System.Runtime.InteropServices;
public class PgProbe {
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr p);
  public delegate bool EnumProc(IntPtr h, IntPtr p);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassNameW(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern IntPtr SendMessage(IntPtr h, uint m, IntPtr w, IntPtr l);
  public static IntPtr Find(string cls) {
    IntPtr f = IntPtr.Zero;
    EnumWindows((h,p) => { var sb=new StringBuilder(256); GetClassNameW(h,sb,256);
      if (sb.ToString()==cls) { f=h; return false; } return true; }, IntPtr.Zero);
    return f;
  }
  public static bool SendEsc(IntPtr h) {
    if (h == IntPtr.Zero) { return false; }
    SendMessage(h, 0x0100, (IntPtr)0x1B, IntPtr.Zero);   // WM_KEYDOWN VK_ESCAPE
    return true;
  }
}
'@

$d3 = New-TempDir 'empty'
if (-not (Wait-NoInstance)) { Write-Host "  WARNING: previous instance still running (case 3)" }
$env:LH_BASE_DIR = $d3
$env:LH_UI_ACTION = 'pages'
$env:LH_PROBE_PAGES = ''
Reset-Diag
$p3 = Start-Process -FilePath $Exe -WorkingDirectory $d3 -PassThru

# Poll for the popup: the app opens it ~3.5s after start, and (in hook mode) closes the
# whole window ~1.8s later -- so a fixed sleep would race it. Poll instead.
$popup = [IntPtr]::Zero
for ($i = 0; $i -lt 40; $i++) {
    Start-Sleep -Milliseconds 250
    $popup = [PgProbe]::Find("LearnHelperPagePickerWnd")
    if ($popup -ne [IntPtr]::Zero) { break }
}
Check "popup window exists before Esc" ($popup -ne [IntPtr]::Zero)

$snap3 = Join-Path $env:TEMP ("lh-pages-diag3-" + (Get-Random) + ".log")
$log3 = Wait-DiagMatch $snap3 'pagepicker: shown items=0'
Check "empty list still opens (items=0)" (@($log3 | Where-Object { $_ -match 'pagepicker: shown items=0' }).Count -gt 0)

[void][PgProbe]::SendEsc([PgProbe]::Find("LearnHelperPagePickerWnd"))
Start-Sleep -Milliseconds 800
$popupAfter = [PgProbe]::Find("LearnHelperPagePickerWnd")
Check "Esc closed the popup" ($popupAfter -eq [IntPtr]::Zero)
Diag-Snapshot $snap3
$log3b = @(Get-Content $snap3 -Encoding UTF8 -ErrorAction SilentlyContinue)
Check "popup destroyed trace present" (@($log3b | Where-Object { $_ -match 'pagepicker: window destroyed' }).Count -gt 0)
$cfg3 = Read-Cfg $d3
Check "Esc did not select anything" ($null -eq $cfg3 -or [string]::IsNullOrEmpty($cfg3.last_page_title))

Start-Sleep -Seconds 3
$left3 = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -like '*learn-helper*' })
if (-not $p3.HasExited) { Stop-Process -Id $p3.Id -Force -ErrorAction SilentlyContinue }
Start-Sleep -Milliseconds 1200
$left3b = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -like '*learn-helper*' })
Check "backend process cleaned up" ($left3b.Count -eq 0) ("before=" + $left3.Count + " after=" + $left3b.Count)
Remove-Item Env:LH_BASE_DIR -ErrorAction SilentlyContinue
Remove-Item Env:LH_UI_ACTION -ErrorAction SilentlyContinue

Write-Host ""
Write-Host ("RESULT: " + $script:pass + " passed, " + $script:fail + " failed")
Write-Host ("temp dir kept for inspection: " + $d1 + " ; " + $d3)
if ($script:fail -gt 0) { exit 1 }
exit 0
