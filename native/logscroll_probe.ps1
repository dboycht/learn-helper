# logscroll_probe.ps1 -- headless end-to-end verification of the log panel scroll bar (2.1.3).
#
# What it proves (nobody clicks anything, and we never sample the screen):
#   1) with more log lines than fit, the self-drawn scroll bar appears
#      (`paint-logs: ... bar=1 track=[...] thumb=[...]` carries the exact geometry)
#   2) hovering the bar highlights it, and the wheel over the bar is NOT stolen
#      (`hover=1` on the bar, while `first` stays put)
#   3) dragging the thumb to the top shows the first line (`first=0`) and stops following
#   4) dragging the thumb back to the bottom resumes following the newest line (`follow=1`)
#   5) clicking the empty track above/below the thumb flips one page
#   6) with fewer lines than fit there is no bar at all (`bar=0`), and clicks in the
#      bar area do nothing (no accidental drag, no scroll)
#
# The evidence is the geometry the painter itself writes to native-diag.log plus one trace per
# gesture, so "what is drawn" and "what is clickable" cannot drift.
#
# It never moves or clicks the REAL mouse: everything is posted messages (that is why the
# hovered/unhovered state is pinned with LH_PROBE_HOVER_BAR instead of being chased with the
# pointer). The highlight's colours are checked separately, mouse-free, in logscroll_hover_check.ps1.
#
# ASCII only (PS 5.1 reads BOM-less scripts as GBK -> a Chinese comment can swallow the
# next line; see DEVELOPMENT.md, iron rule 8).

param(
    [string]$Exe = "D:\code\DeepSeekHarness\learn-helper\native\target\release\learn-helper-native.exe",
    [int]$WaitSeconds = 45
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

function New-TempDir([string]$tag) {
    $d = Join-Path $env:TEMP ("lh-logscroll-" + $tag + "-" + (Get-Random))
    New-Item -ItemType Directory -Path $d -Force | Out-Null
    return $d
}

function Reset-Diag() {
    Remove-Item $script:diag -Force -ErrorAction SilentlyContinue
}

function Diag-Snapshot([string]$dest) {
    if (Test-Path $script:diag) { Copy-Item $script:diag $dest -Force }
    else { Set-Content -Path $dest -Value '' -Encoding ASCII }
}

function Diag-Lines([string]$dest) {
    Diag-Snapshot $dest
    return @(Get-Content $dest -Encoding UTF8 -ErrorAction SilentlyContinue)
}

# Wait until a trace line matching $pattern shows up, then return that run's lines.
# Asserts on evidence that has ARRIVED instead of sampling one instant (E46/E48).
function Wait-DiagMatch([string]$dest, [string]$pattern, [int]$timeoutMs = 6000) {
    $deadline = (Get-Date).AddMilliseconds($timeoutMs)
    while ((Get-Date) -lt $deadline) {
        $lines = Diag-Lines $dest
        if (@($lines | Where-Object { $_ -match $pattern }).Count -gt 0) { return $lines }
        Start-Sleep -Milliseconds 150
    }
    return (Diag-Lines $dest)
}

# The app is single-instance (named mutex): a new process exits(0) while the old one is
# still shutting down, which makes EVERY assertion fail and looks like "it is all broken".
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

# Parse the LAST "paint-logs:" line and pull one named field out of it.
function Field([object[]]$log, [string]$key) {
    $line = @($log | Where-Object { $_ -match 'paint-logs:' })[-1]
    if (-not $line) { return $null }
    if ($line -match ($key + '=(-?\d+)')) { return [int]$Matches[1] }
    return $null
}

function AnyPaintLogs([object[]]$log) {
    return (@($log | Where-Object { $_ -match 'paint-logs:' }).Count -gt 0)
}

Add-Type -TypeDefinition @'
using System;
using System.Text;
using System.Runtime.InteropServices;
public class LhProbe {
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr p);
  public delegate bool EnumProc(IntPtr h, IntPtr p);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassNameW(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern IntPtr FindWindowW(string cls, string title);
  [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr h, uint m, IntPtr w, IntPtr l);
  [DllImport("user32.dll")] public static extern bool GetClientRect(IntPtr h, out RECT r);

  public struct RECT { public int Left, Top, Right, Bottom; }

  // CRITICAL: this probe process must be per-monitor DPI aware BEFORE it does anything with
  // window rectangles. A DPI-unaware process gets VIRTUALIZED coordinates: GetClientRect
  // returned 1124x640 for a window that is really 1686x960, and every posted message was
  // scaled on the way in -- clicks landed far outside the log scroll bar and the whole run
  // looked like "the feature is broken" (see DEVELOPMENT.md, iron rule 12 / E49).
  [DllImport("user32.dll")] public static extern bool SetProcessDpiAwarenessContext(IntPtr ctx);
  [DllImport("shcore.dll")] public static extern int SetProcessDpiAwareness(int value);

  public static bool MakeDpiAware() {
    // DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 == ((HANDLE)-4)
    try { if (SetProcessDpiAwarenessContext(new IntPtr(-4))) { return true; } } catch { }
    try { if (SetProcessDpiAwareness(2) == 0) { return true; } } catch { }   // 2 = PER_MONITOR
    return false;
  }

  public static string ClientSize(IntPtr h) {
    RECT r;
    if (!GetClientRect(h, out r)) { return "?"; }
    return (r.Right - r.Left) + "x" + (r.Bottom - r.Top);
  }

  public static IntPtr Find(string cls) {
    IntPtr f = IntPtr.Zero;
    EnumWindows((h,p) => { var sb=new StringBuilder(256); GetClassNameW(h,sb,256);
      if (sb.ToString()==cls) { f=h; return false; } return true; }, IntPtr.Zero);
    if (f == IntPtr.Zero) { f = FindWindowW(cls, null); }
    return f;
  }
  public static void Move(IntPtr h, int x, int y) {
    PostMessage(h, 0x0200, IntPtr.Zero, (IntPtr)((y << 16) | (x & 0xFFFF)));          // WM_MOUSEMOVE
  }
  // NOTE: this probe NEVER moves or clicks the real mouse (the user asked us not to hijack
  // their pointer). Everything goes through posted messages plus the app's own traces.
  // Consequence: the hover state can be overwritten at any moment by a GENUINE WM_MOUSEMOVE at
  // the physical cursor position, so hover is NOT asserted from a sampled dump -- it is asserted
  // from the action trace of the click/wheel that consumed it (see the bar-down trace), and the
  // highlight's colours are verified separately with --render-probe (also mouse-free).
  // Forces one repaint so a fresh "paint-logs:" line is written with NO state change.
  // Needed because the app only invalidates the window when something actually changed:
  // after a Reset-Diag, a no-op mouse move produces no dump at all and the probe would keep
  // reading the previous dump (that is how "hover=1" turned into a bogus PASS in run #1).
  public static void Repaint(IntPtr h) {
    PostMessage(h, 0x8001, IntPtr.Zero, IntPtr.Zero);                                 // WM_APP_BACKEND
  }
  public static void Down(IntPtr h, int x, int y) {
    PostMessage(h, 0x0201, (IntPtr)1, (IntPtr)((y << 16) | (x & 0xFFFF)));           // WM_LBUTTONDOWN
  }
  public static void Up(IntPtr h, int x, int y) {
    PostMessage(h, 0x0202, IntPtr.Zero, (IntPtr)((y << 16) | (x & 0xFFFF)));         // WM_LBUTTONUP
  }
  // WM_MOUSEWHEEL: HIWORD(wparam) = signed wheel delta. The app never reads the LPARAM
  // screen coordinates on this path, so they are left at 0.
  public static void Wheel(IntPtr h, int delta) {
    int wp = (delta << 16);
    PostMessage(h, 0x020A, (IntPtr)wp, IntPtr.Zero);
  }
}
'@

$dpiAware = [LhProbe]::MakeDpiAware()
Write-Host ("dpi aware = " + $dpiAware)

Write-Host "=== log panel scroll bar probe ==="
Write-Host ("exe  = " + $Exe)
Write-Host ("diag = " + $script:diag)
if (-not (Test-Path $Exe)) { Write-Host "EXE not found"; exit 2 }

$running = @(Get-Process -Name 'learn-helper-native' -ErrorAction SilentlyContinue)
if ($running.Count -gt 0) {
    Write-Host "ABORT: learn-helper-native already running (single-instance mutex)"
    exit 3
}

function Start-Case([string]$dir, [string]$logs, [string]$exePath = $Exe, [string]$hoverPin = '', [string]$noBackend = '') {
    if (-not (Wait-NoInstance)) { Write-Host "  WARNING: previous instance still running" }
    $env:LH_BASE_DIR = $dir
    # Guard: this probe does not test the auto-launch-browser feature, and the app arms that
    # feature 4 seconds after start. Several probes run longer than that, so without this the
    # app could really open the user's sandbox browser in the middle of a test (found by audit).
    $env:LH_NO_AUTO_BROWSER = '1'
    $env:LH_PROBE_LOGS = $logs
    Remove-Item Env:LH_UI_ACTION -ErrorAction SilentlyContinue
    # NOTE: these must be in the environment BEFORE Start-Process: a child inherits the environment
    # block captured at creation time, so setting a variable afterwards has no effect on it
    # (that mistake made the hover pin look broken -- the pinned run behaved exactly like an
    # unpinned one because the app had never seen the variable; 2.1.3, E50).
    if ($hoverPin -ne '') { $env:LH_PROBE_HOVER_BAR = $hoverPin }
    else { Remove-Item Env:LH_PROBE_HOVER_BAR -ErrorAction SilentlyContinue }
    # `LH_NO_BACKEND=1` = do not spawn the Python backend at all. Needed by the short-log case:
    # with a live backend the panel keeps gaining lines (handshake / status pollers) and
    # "everything still fits, so there is no bar" stops being true a few seconds later.
    # The older workaround -- running a COPY of the exe from a folder without backend/ -- is
    # unreliable: locate_backend() walks UP the directory tree and still found the real backend
    # from a %TEMP% subfolder (that is why this case failed; 2.1.3, E54).
    if ($noBackend -ne '') { $env:LH_NO_BACKEND = $noBackend }
    else { Remove-Item Env:LH_NO_BACKEND -ErrorAction SilentlyContinue }
    # The diag log lives next to the exe, so each case may have its own copy (see CASE 3).
    $script:diag = Join-Path (Split-Path -Parent $exePath) 'native-diag.log'
    Reset-Diag
    $p = Start-Process -FilePath $exePath -WorkingDirectory $dir -PassThru
    # Wait for the window itself: MainWindowHandle is empty right after Start-Process and
    # posting messages at a null hwnd silently does nothing (a "everything fails" run).
    $deadline = (Get-Date).AddSeconds(20)
    $h = [IntPtr]::Zero
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 300
        try { $p.Refresh() } catch { }
        if ($p.HasExited) { break }
        $h = [LhProbe]::Find("LearnHelperNativeWnd")
        if ($h -ne [IntPtr]::Zero) { break }
    }
    return @{ Proc = $p; Hwnd = $h }
}

function Stop-Case($case, [string]$snap) {
    $p = $case.Proc
    if (-not $p.HasExited) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Milliseconds 900
    $lines = Diag-Lines $snap
    Remove-Item Env:LH_BASE_DIR -ErrorAction SilentlyContinue
    Remove-Item Env:LH_PROBE_LOGS -ErrorAction SilentlyContinue
    Remove-Item Env:LH_PROBE_HOVER_BAR -ErrorAction SilentlyContinue
    return $lines
}

# ------------------------------------------------------------------ CASE 1 + 2: long log
$many = @(1..60 | ForEach-Object { "probe line " + $_ }) -join '|'
$d1 = New-TempDir 'long'
$snap1 = Join-Path $env:TEMP ("lh-logscroll-1-" + (Get-Random) + ".log")
$case1 = Start-Case $d1 $many
$h1 = $case1.Hwnd
Check "main window found" ($h1 -ne [IntPtr]::Zero)
$log1 = Wait-DiagMatch $snap1 'paint-logs:'

Write-Host ""
Write-Host "CASE 1: more lines than fit -> the bar exists and the thumb sits at the bottom"
Check "probe lines injected" (@($log1 | Where-Object { $_ -match 'LH_PROBE_LOGS injected 60 line' }).Count -gt 0)
Check "paint-logs geometry emitted" (AnyPaintLogs $log1)
Check "bar shown when overflowing (bar=1)" ((Field $log1 'bar') -eq 1)
Check "starts in follow mode (follow=1)" ((Field $log1 'follow') -eq 1)
Check "60 injected + 1 startup line = 61 rows" ((Field $log1 'total') -eq 61)
Check "thumb leaves room to travel (thumb shorter than track)" ((Field $log1 'thumb_h') -lt (Field $log1 'track_h')) ("thumb_h=" + (Field $log1 'thumb_h') + " track_h=" + (Field $log1 'track_h'))

# The bar geometry comes from the painter itself, so the probe always clicks where the
# bar really is -- at 150% DPI the window is bigger and hard-coded pixels would miss it.
# x = middle of the track; CASE 3 reuses it to click "where the bar would be" (same window
# size in both cases, so the same numbers apply).
$trackLeft = Field $log1 'track_left'
$trackTop = Field $log1 'track_top'
$trackH = Field $log1 'track_h'
$trackX = $trackLeft + 7

# Guard against the "two coordinate systems" trap: if this probe were DPI-unaware,
# GetClientRect would report a smaller, virtualized client (1124x640 instead of 1686x960)
# and every posted message would be scaled on the way in, so the clicks would land far
# outside the bar and the whole run would look like "the feature is broken" (E49).
$geomLine = @($log1 | Where-Object { $_ -match 'geometry self=\d+x\d+ client=' })[-1]
$selfSize = ''
$clientW = $null
if ($geomLine -and $geomLine -match 'self=(\d+)x(\d+)') { $selfSize = $Matches[1] + 'x' + $Matches[2] }
if ($geomLine -and $geomLine -match 'client=(\d+)x(\d+)') { $clientW = [int]$Matches[1] }
$probeClient = [LhProbe]::ClientSize($h1)
Check "probe and app see the same client size (no DPI virtualization)" ($selfSize -ne '' -and $probeClient -eq $selfSize) ("probe=" + $probeClient + " app=" + $selfSize)
Check "scroll bar lies inside the client area" ($null -ne $clientW -and $trackLeft -gt 0 -and ($trackLeft + 15) -le $clientW) ("trackLeft=" + $trackLeft + " clientW=" + $clientW)

# A point in the log text well left of the bar -- "pointer is NOT on the bar, but is also not
# on a button".  Derived from the bar geometry so it stays correct at any DPI.
$logPadX = [int]($trackLeft / 2)
$logPadY = $trackTop + 40

Write-Host ""
Write-Host "CASE 2: wheel, hover and drag"

# --- wheel up: asserted on the ACTION TRACE, not on a sampled dump.
# The app appends a "ui: log wheel up first A -> B (max=C) follow=0" line inside the handler,
# which is the value at the exact moment of the wheel -- immune to the background log lines
# that keep arriving every few seconds (comparing two dumps after the fact is not: the viewport
# is anchored to the visible log, so new lines shift it; that produced a flaky expectation).
Reset-Diag
[LhProbe]::Move($h1, $logPadX, $logPadY)
Start-Sleep -Milliseconds 300
[LhProbe]::Repaint($h1)
$logBase = Wait-DiagMatch $snap1 'paint-logs:'
Check "moving into the log text clears the bar hover (hover=0)" ((Field $logBase 'hover') -eq 0) ("hover=" + (Field $logBase 'hover'))
$before = Field $logBase 'first'
[LhProbe]::Wheel($h1, 120)
Start-Sleep -Milliseconds 600
$log2 = Diag-Lines $snap1
$trace2 = @($log2 | Where-Object { $_ -match 'log wheel up' })[-1]
$wheelOk = $false
$wheelDetail = "trace not found"
if ($trace2 -and $trace2 -match 'first (\d+) -> (\d+) \(max=(\d+)\) follow=(\d)') {
    $fa = [int]$Matches[1]; $fb = [int]$Matches[2]; $mx = [int]$Matches[3]; $fl = [int]$Matches[4]
    $want = [Math]::Max(0, $fa - 3)
    $wheelOk = ($fb -eq $want) -and ($fl -eq 0)
    $wheelDetail = "first $fa -> $fb (expected $want, max=$mx) follow=$fl"
}
Check "wheel up moves the viewport back exactly 3 lines and stops following" $wheelOk $wheelDetail
Check "the sampled viewport really moved back too" ((Field $log2 'first') -lt $before) ("before=" + $before + " now=" + (Field $log2 'first'))

# --- drag the thumb to the very top: press it, move to the track top, release.
# Re-read the CURRENT geometry first: background log lines kept arriving, so the thumb has moved
# since the earlier dump (the bar is always pinned to the newest line while following).
Reset-Diag
[LhProbe]::Repaint($h1)
Start-Sleep -Milliseconds 400
$logNow = Wait-DiagMatch $snap1 'paint-logs:'
$thumbTop = Field $logNow 'thumb_top'
$thumbH = Field $logNow 'thumb_h'
Check "thumb geometry is sane" ($null -ne $thumbTop -and $thumbH -gt 0 -and $null -ne $trackTop -and $trackH -gt 0) ("thumbTop=" + $thumbTop + " thumbH=" + $thumbH + " trackTop=" + $trackTop + " trackH=" + $trackH)
$grabY = $thumbTop + [int]($thumbH / 2)

# Every phase below resets the evidence and forces one repaint before reading, so the numbers
# always describe the action that just happened (never a leftover line).
Reset-Diag
[LhProbe]::Down($h1, $trackX, $grabY)
Start-Sleep -Milliseconds 300
$logDown = Diag-Lines $snap1
$downTrace = @($logDown | Where-Object { $_ -match 'log scroll bar down' })[-1]
Check "pressing the thumb is recognised as a thumb grab" ($downTrace -and $downTrace -match 'zone=thumb') ("trace=" + $downTrace)
[LhProbe]::Move($h1, $trackX, ($trackTop + 2))
Start-Sleep -Milliseconds 400
# Re-send the SAME move right before releasing. Reason: a genuine WM_MOUSEMOVE (the OS reports the
# physical cursor position, which the user owns) can land between our move and our release, and
# during a drag that genuine message legitimately re-computes the position -- so the release would
# record whatever the real cursor implied (observed: a stale/off-screen y=65216 -> "drag to top"
# ended up following the tail again). Posting the intended position last pins the drag where the
# probe wants it, without touching the user's mouse.
[LhProbe]::Move($h1, $trackX, ($trackTop + 2))
Start-Sleep -Milliseconds 200
[LhProbe]::Up($h1, $trackX, ($trackTop + 2))
Start-Sleep -Milliseconds 400
$logTop = Diag-Lines $snap1
$topTrace = @($logTop | Where-Object { $_ -match 'log scroll bar drag end' })[-1]
$topFirst = $null
$topFollow = $null
if ($topTrace -and $topTrace -match 'first=(\d+)/(\d+) follow=(\d)') {
    $topFirst = [int]$Matches[1]; $topFollow = [int]$Matches[3]
}
# Asserted on the release trace, not on `drag=1` from a dump: the drag can start and end between two
# repaints (the panel is idle when nothing else changes), so the dump is not a reliable sampler here.
Check "dragging to the top shows the first lines (first<=2)" (($null -ne $topFirst) -and ($topFirst -le 2)) ("trace=" + $topTrace)
Check "still not following after dragging to the top" ($topFollow -eq 0) ("trace=" + $topTrace)

# --- drag it back to the very bottom: following must resume.
# Asserted on the drag traces too: `first` in a dump is a snapshot, and the log keeps growing.
# Same "post the intended position last" trick as above (a genuine mouse move can interleave).
Reset-Diag
[LhProbe]::Down($h1, $trackX, ($trackTop + 2))
Start-Sleep -Milliseconds 300
[LhProbe]::Move($h1, $trackX, ($trackTop + $trackH + 40))
Start-Sleep -Milliseconds 300
[LhProbe]::Move($h1, $trackX, ($trackTop + $trackH + 40))
Start-Sleep -Milliseconds 200
$logDragDown = Diag-Lines $snap1
[LhProbe]::Up($h1, $trackX, ($trackTop + $trackH + 40))
Start-Sleep -Milliseconds 400
$logUp2 = Wait-DiagMatch $snap1 'drag end'
Check "release ends the drag (drag end trace)" (@($logUp2 | Where-Object { $_ -match 'log scroll bar drag end' }).Count -gt 0)
$endTrace = @($logUp2 | Where-Object { $_ -match 'log scroll bar drag end' })[-1]
$endFollow = $null
$endFirst = $null
$endMax = $null
# Trace format: "ui: log scroll bar drag end at y=946 -> first=60/60 follow=1"
if ($endTrace -and $endTrace -match 'first=(\d+)/(\d+) follow=(\d)') {
    $endFirst = [int]$Matches[1]; $endMax = [int]$Matches[2]; $endFollow = [int]$Matches[3]
}
Check "dragging to the bottom resumes following (follow=1)" ($endFollow -eq 1) ("trace=" + $endTrace)
Check "the drag reached the newest line (first = max)" (($null -ne $endFirst) -and ($endFirst -eq $endMax)) ("trace=" + $endTrace)
[LhProbe]::Repaint($h1)
Start-Sleep -Milliseconds 400
$logEnd = Wait-DiagMatch $snap1 'paint-logs:'
Check "the following viewport is pinned to the newest line" ((Field $logEnd 'follow') -eq 1) ("follow=" + (Field $logEnd 'follow') + " first=" + (Field $logEnd 'first') + " total=" + (Field $logEnd 'total'))

# Click the empty track ABOVE the thumb: one page up (the thumb is at the bottom again).
# The bar-down trace carries the exact before/after of the page, so no sampling is involved.
Reset-Diag
[LhProbe]::Down($h1, $trackX, ($trackTop + 2))
Start-Sleep -Milliseconds 250
[LhProbe]::Up($h1, $trackX, ($trackTop + 2))
Start-Sleep -Milliseconds 400
$logPage = Diag-Lines $snap1
$pageTrace = @($logPage | Where-Object { $_ -match 'log scroll bar down' })[-1]
$pageOk = $false
if ($pageTrace -and $pageTrace -match 'zone=(\S+) first (\d+) -> (\d+) \(max=(\d+)\) follow=(\d) page=(\d+)') {
    $zone = $Matches[1]; $pf = [int]$Matches[2]; $pt = [int]$Matches[3]; $pageN = [int]$Matches[6]
    $pageOk = ($zone -eq 'track-above') -and ($pt -eq [Math]::Max(0, $pf - $pageN))
    Check "track click above the thumb pages up by one screen" $pageOk ("zone=" + $zone + " first " + $pf + " -> " + $pt + " page=" + $pageN)
    Check "paging stops following" ($Matches[5] -eq '0') ("follow=" + $Matches[5])
} else {
    Check "track click above the thumb pages up by one screen" $false ("trace not found: " + $pageTrace)
}

$log1b = Stop-Case $case1 $snap1

# ------------------------------------------------------------------ CASE 2b: pinned pointer
Write-Host ""
Write-Host "CASE 2b: pointer pinned onto the bar -> the wheel must be swallowed"
# The pointer must be pinned AT LAUNCH (`LH_PROBE_HOVER_BAR=1`, honoured only when the variable is
# set -- see Start-Case): a child inherits its environment when it is created, so it cannot be
# switched on mid-run, and chasing the real cursor is off-limits (it is the user's). This case runs
# on its own instance, after CASE 1 has exited (single-instance mutex). The pixel-level proof that
# the pinned state really highlights the bar is in logscroll_hover_check.ps1.
$d2b = New-TempDir 'pinned'
$snap2b = Join-Path $env:TEMP ("lh-logscroll-2b-" + (Get-Random) + ".log")
$case2b = Start-Case $d2b $many $Exe '1'
$h2b = $case2b.Hwnd
Check "main window found (pinned case)" ($h2b -ne [IntPtr]::Zero)
$log2b = Wait-DiagMatch $snap2b 'paint-logs:'
Check "pointer pinned onto the bar is reported (paint-logs hover=1)" (@($log2b | Where-Object { $_ -match 'paint-logs:.*hover=1' }).Count -gt 0)
Check "the pinned case really has a bar (bar=1)" ((Field $log2b 'bar') -eq 1)
$firstOnBar = Field $log2b 'first'
Reset-Diag
[LhProbe]::Repaint($h2b)
Start-Sleep -Milliseconds 300
[LhProbe]::Wheel($h2b, 120)
[LhProbe]::Wheel($h2b, 120)
Start-Sleep -Milliseconds 500
$logW2b = Diag-Lines $snap2b
Check "wheel over the bar is swallowed (no wheel trace at all)" (@($logW2b | Where-Object { $_ -match 'log wheel ' }).Count -eq 0) ("wheel traces=" + @($logW2b | Where-Object { $_ -match 'log wheel ' }).Count)
# It must not scroll BACKWARDS. It may drift forwards on its own: following mode keeps the viewport
# pinned to the newest line, and the backend keeps appending lines while the case runs (the bar also
# marks where each request was handled, which is useful -- the point is that the wheel did nothing).
$firstAfter = Field $logW2b 'first'
Check "the wheel did not scroll the log backwards" (($null -ne $firstOnBar) -and ($null -ne $firstAfter) -and ($firstAfter -ge $firstOnBar)) ("before=" + $firstOnBar + " now=" + $firstAfter + " (max=" + (Field $logW2b 'total') + " visible=" + (Field $logW2b 'visible') + ")")
$log2bEnd = Stop-Case $case2b $snap2b

# ------------------------------------------------------------------ CASE 3: short log
Write-Host ""
Write-Host "CASE 3: fewer lines than fit -> no bar, and the bar area is inert"
# This case needs a log that does NOT grow while we test it, so it runs a COPY of the exe from
# a directory with no backend/ next to it: locate_backend() searches upward from the exe, so
# This case needs a log that does NOT grow while we test it, so the backend is turned off
# entirely with `LH_NO_BACKEND=1` (see Start-Case). With a live backend the panel kept gaining
# lines (handshake / status pollers), and after a few seconds it overflowed -- the "no bar" case
# silently turned into "bar present" and the assertions measured the wrong thing.
$d3 = New-TempDir 'short'
$few = @(1..3 | ForEach-Object { "short line " + $_ }) -join '|'
$snap3 = Join-Path $env:TEMP ("lh-logscroll-3-" + (Get-Random) + ".log")
$case3 = Start-Case $d3 $few $Exe '' '1'
$h3 = $case3.Hwnd
Check "main window found (short case, no backend)" ($h3 -ne [IntPtr]::Zero)
Check "backend really skipped for this case" ((@(Get-Content $script:diag -Encoding UTF8 -ErrorAction SilentlyContinue | Where-Object { $_ -match 'LH_NO_BACKEND' }).Count) -gt 0)
Start-Sleep -Seconds 4   # let any startup chatter settle; no backend means nothing more arrives
Reset-Diag
[LhProbe]::Move($h3, $logPadX, $logPadY)
Start-Sleep -Milliseconds 300
[LhProbe]::Repaint($h3)
$log3 = Wait-DiagMatch $snap3 'paint-logs:'
Check "paint-logs geometry emitted (short case)" (AnyPaintLogs $log3)
Check "no bar when everything fits (bar=0)" ((Field $log3 'bar') -eq 0)
Check "no phantom drag from a previous case (drag=0)" ((Field $log3 'drag') -eq 0)
Check "nothing to scroll (follow=1, first=0)" (((Field $log3 'follow') -eq 1) -and ((Field $log3 'first') -eq 0)) ("follow=" + (Field $log3 'follow') + " first=" + (Field $log3 'first'))

# Click exactly where the bar WOULD be, then wheel. Nothing may happen.
# The pointer is pinned onto the bar for this run (see the note in CASE 2): that is the ONLY
# place where the wheel is allowed to be swallowed, so it must still be swallowed here... except
# that there is no bar at all, which is exactly what "no drag, no scroll, still following" shows.
Reset-Diag
$env:LH_PROBE_HOVER_BAR = '1'
[LhProbe]::Move($h3, $trackX, ($trackTop + 100))
Start-Sleep -Milliseconds 300
[LhProbe]::Down($h3, $trackX, ($trackTop + 100))
Start-Sleep -Milliseconds 200
[LhProbe]::Up($h3, $trackX, ($trackTop + 100))
Start-Sleep -Milliseconds 300
[LhProbe]::Wheel($h3, 120)
[LhProbe]::Wheel($h3, 120)
Start-Sleep -Milliseconds 500
$log3b = Diag-Lines $snap3
Check "click in the bar area starts no drag (no bar-down trace)" (@($log3b | Where-Object { $_ -match 'log scroll bar down' }).Count -eq 0)
Check "wheel in the bar area does not scroll (first stays 0)" ((Field $log3b 'first') -eq 0) ("first=" + (Field $log3b 'first'))
# NOTE: `follow` must NOT be asserted as "still 1" here. With nothing to overflow, max_first is 0,
# so a wheel-down clamps first to 0 == max_first and the "you reached the bottom again" rule
# legitimately turns following ON -- asserting follow=1 before the wheel was simply wrong on my
# side (and a wheel-up correctly turns it off). What matters is that the viewport never moved and
# a following viewport really is pinned to the newest line.
# Expected "first" is max(0, total - visible): when fewer lines than fit exist there is nothing
# to scroll and `first` is 0, whereas `total - visible` goes NEGATIVE. The probe compared against
# the raw difference, so it failed whenever the injected log lines had not all arrived yet (a race
# on `total`) -- exactly the "flaky probe" class this project hates.
$expFirst3 = [Math]::Max(0, ((Field $log3b 'total') - (Field $log3b 'visible')))
Check "the viewport is pinned to the newest line" ((Field $log3b 'first') -eq $expFirst3) ("first=" + (Field $log3b 'first') + " total=" + (Field $log3b 'total') + " visible=" + (Field $log3b 'visible') + " expected=" + $expFirst3)
Remove-Item Env:LH_PROBE_HOVER_BAR -ErrorAction SilentlyContinue

$log3c = Stop-Case $case3 $snap3

# ------------------------------------------------------------------ backend cleanup
Start-Sleep -Seconds 2
$left = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -like '*learn-helper*' })
Check "backend process cleaned up" ($left.Count -eq 0) ("left=" + $left.Count)

Write-Host ""
Write-Host ("RESULT: " + $script:pass + " passed, " + $script:fail + " failed")
Write-Host ("temp dirs kept for inspection: " + $d1 + " ; " + $d3)
if ($script:fail -gt 0) { exit 1 }
exit 0
