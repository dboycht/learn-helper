# drag_probe.ps1 -- verify that dragging the title bar MOVES the window in step with the mouse.
#
# Rule compliance (AGENTS.md rule 12 / DEVELOPMENT.md iron rule 12): this probe must never touch
# the user's real mouse or keyboard. It used to (SetCursorPos + SendInput) -- that was a breach,
# and it also left the physical button down if any step threw. It now POSTS window messages, which
# exercises exactly the same code path in the app (the drag logic reads the grab point and the
# cursor position out of WM_LBUTTONDOWN / WM_MOUSEMOVE's LPARAMs).
#
# How the assertion works, and why it is not simply "did the window move":
#   * The app writes one line per applied move, gated behind LH_TRACE_DRAG=1:
#         ui: drag apply dx=<computed> dy=<computed> delivered=(<applied>) rect=(...) win0=(...)
#   * "delivered" must equal the computed dx/dy (the horizontal part exactly; the vertical part may
#     be smaller because the app deliberately clamps the window into the monitor work area).
#     That invariant is precisely what ERROR.md E75 broke: the computed delta collapsed to ~0, so
#     the window did not follow the mouse and `delivered` was (0,0).
#   * We deliberately do NOT assert on the window's final position: the app cannot distinguish our
#     posted WM_MOUSEMOVE from the user's real one (by design), so any physical mouse movement
#     during the run contaminates the final rect (observed: dx=434 appeared between posted steps of
#     dx=60). The applied offsets are computed from OUR messages only, so they are deterministic.
#
# Prerequisite: the app must be running and started with LH_TRACE_DRAG=1, e.g.
#     $env:LH_TRACE_DRAG='1'; $env:LH_NO_AUTO_BROWSER='1'
#     Start-Process D:\code\DeepSeekHarness\learn-helper\native\target\release\learn-helper-native.exe
#
# Usage: powershell -ExecutionPolicy Bypass -File drag_probe.ps1 [-Steps 3] [-Dx 120] [-Dy 60]
# ASCII only (rules/01 section 8.2).

param(
    [int]$Steps = 6,
    [int]$Dx = 120,
    [int]$Dy = 60
)

$ErrorActionPreference = 'Continue'

$code = @'
using System;
using System.Text;
using System.Runtime.InteropServices;
public class DP {
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr p);
  public delegate bool EnumProc(IntPtr h, IntPtr p);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassNameW(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool GetClientRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
  [DllImport("user32.dll")] public static extern IntPtr SendMessage(IntPtr h, uint msg, IntPtr wp, IntPtr lp);
  [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr h, uint msg, IntPtr wp, IntPtr lp);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  // The probe itself MUST be DPI aware: otherwise Windows virtualizes its coordinates and the
  // rects we read are logical while the app uses physical ones (ERROR.md E37).
  // Keep this here-string pure ASCII: PS 5.1 decodes a BOM-less UTF-8 script as GBK and a stray
  // byte from a Chinese comment can swallow the NEXT line (E43).
  [DllImport("user32.dll")] public static extern bool SetProcessDpiAwarenessContext(IntPtr ctx);
  public static readonly IntPtr DPI_PER_MONITOR_AWARE_V2 = new IntPtr(-4);

  public const uint WM_LBUTTONDOWN = 0x0201;
  public const uint WM_LBUTTONUP   = 0x0202;
  public const uint WM_MOUSEMOVE   = 0x0200;
  public const int  MK_LBUTTON     = 0x0001;

  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }

  public static IntPtr FindByClass(string cls) {
    IntPtr found = IntPtr.Zero;
    EnumWindows((h, p) => {
      var sb = new StringBuilder(256);
      GetClassNameW(h, sb, 256);
      if (sb.ToString() == cls) { found = h; return false; }
      return true;
    }, IntPtr.Zero);
    return found;
  }

  public static string Rect(IntPtr h) {
    RECT r;
    if (!GetWindowRect(h, out r)) return "(GetWindowRect failed)";
    return string.Format("{0},{1} {2}x{3} visible={4} iconic={5}",
      r.Left, r.Top, r.Right - r.Left, r.Bottom - r.Top, IsWindowVisible(h), IsIconic(h));
  }

  // Pack client coords exactly like the OS does for mouse messages.
  public static IntPtr MakeLParam(int x, int y) {
    return (IntPtr)((y << 16) | (x & 0xFFFF));
  }

  public static IntPtr hwnd = IntPtr.Zero;

  // down -> moves -> up, all posted so the app's own loop processes them in order.
  public static string Drag(int cx, int cy, int steps, int dx, int dy) {
    var log = new StringBuilder();
    PostMessage(hwnd, WM_LBUTTONDOWN, (IntPtr)MK_LBUTTON, MakeLParam(cx, cy));
    log.AppendLine("DOWN       : client=" + cx + "," + cy);
    int px = cx, py = cy;
    for (int i = 1; i <= steps; i++) {
      px += dx; py += dy;
      PostMessage(hwnd, WM_MOUSEMOVE, (IntPtr)MK_LBUTTON, MakeLParam(px, py));
      System.Threading.Thread.Sleep(160);   // let the app process it and write its trace
      log.AppendLine(string.Format("STEP {0,-2}    : client={1},{2}  window={3}", i, px, py, Rect(hwnd)));
    }
    PostMessage(hwnd, WM_LBUTTONUP, (IntPtr)0, MakeLParam(px, py));
    log.AppendLine("UP         : client=" + px + "," + py);
    return log.ToString();
  }
}
'@
Add-Type -TypeDefinition $code

function Get-AppliedTraces {
    param([string]$LogPath)
    $out = New-Object System.Collections.ArrayList
    if (-not (Test-Path $LogPath)) { return $out }
    foreach ($line in [System.IO.File]::ReadAllLines($LogPath)) {
        if ($line -match 'ui: drag apply dx=(-?\d+) dy=(-?\d+) delivered=\((-?\d+),(-?\d+)\)') {
            $null = $out.Add([pscustomobject]@{
                ComputedX = [int]$Matches[1]
                ComputedY = [int]$Matches[2]
                AppliedX = [int]$Matches[3]
                AppliedY = [int]$Matches[4]
            })
        }
    }
    return $out
}

# --- locate the app (and its diagnostics log, which sits next to the exe) ---
$exeDir = Split-Path -Parent $PSScriptRoot   # native\target\release
$exe = Join-Path $PSScriptRoot 'target\release\learn-helper-native.exe'
$diag = Join-Path $exeDir 'target\release\native-diag.log'
if (-not (Test-Path $diag)) { $diag = Join-Path (Split-Path -Parent $exe) 'native-diag.log' }

[void][DP]::SetProcessDpiAwarenessContext([DP]::DPI_PER_MONITOR_AWARE_V2)

$hwnd = [DP]::FindByClass("LearnHelperNativeWnd")
if ($hwnd -eq [IntPtr]::Zero) {
    Write-Output "NO_WINDOW"
    Write-Output "the app must be running with tracing enabled:"
    Write-Output "    `$env:LH_TRACE_DRAG='1'; `$env:LH_NO_AUTO_BROWSER='1'"
    Write-Output ("    Start-Process '" + $exe + "'")
    exit 1
}
[DP]::hwnd = $hwnd
Write-Output ("LOGFILE    : " + $diag + " (exists=" + (Test-Path $diag) + ")")

[void][DP]::SetForegroundWindow($hwnd)
Start-Sleep -Milliseconds 600
Write-Output ("START      : " + [DP]::Rect($hwnd))

$cr = [DP+RECT]::new()
[void][DP]::GetClientRect($hwnd, [ref]$cr)
$cx = [int]($cr.Right / 2)
$cy = 25
Write-Output ("GRAB POINT : client={0},{1}" -f $cx, $cy)

$before = @(Get-AppliedTraces -LogPath $diag).Count
Write-Output ([DP]::Drag($cx, $cy, $Steps, $Dx, $Dy))
Start-Sleep -Milliseconds 500
Write-Output ("END        : " + [DP]::Rect($hwnd))

# --- assertion over OUR traces (everything appended during this run) ---
$all = @(Get-AppliedTraces -LogPath $diag)
$ours = @($all | Select-Object -Skip $before)

$wantX = $Dx * $Steps
$wantY = $Dy * $Steps
$sawOurStep = $false
$bad = @()
foreach ($t in $ours) {
    # The invariant: what the app APPLIED must equal what it COMPUTED, except that the vertical
    # part may legitimately be smaller (the app clamps the window into the monitor work area).
    # This also catches the special case of a downward drag that is entirely clamped (applied 0
    # while computed > 0) -- which is correct behaviour, not a failure, hence the direction check.
    $xOk = ($t.AppliedX -eq $t.ComputedX)
    if ($t.ComputedY -eq 0) { $yOk = ($t.AppliedY -eq 0) }
    elseif ($t.ComputedY -gt 0) { $yOk = ($t.AppliedY -le $t.ComputedY -and $t.AppliedY -ge 0) }
    else { $yOk = ($t.AppliedY -ge $t.ComputedY -and $t.AppliedY -le 0) }
    if (-not ($xOk -and $yOk)) {
        $bad += ("computed=($($t.ComputedX),$($t.ComputedY)) applied=($($t.AppliedX),$($t.AppliedY))")
    }
    # Seeing our exact posted displacement in the app's own trace proves the messages reached it.
    if ($t.ComputedX -eq $wantX -and $t.ComputedY -eq $wantY) { $sawOurStep = $true }
}

Write-Output ("DRAG TRACE : " + $ours.Count + " line(s) this run; sawOurStep=" + $sawOurStep)
if ($bad.Count -gt 0) { Write-Output ("             INCONSISTENT: " + (($bad | Select-Object -First 3) -join ' | ')) }
Write-Output ("EXPECT     : posted displacement dx=" + $wantX + " dy=" + $wantY)

# Pass requires: the app computed exactly our posted displacement (so the messages arrived and the
# arithmetic is right) and every applied offset was consistent with its computed one.
$ok = $sawOurStep -and ($bad.Count -eq 0)
if ($ours.Count -eq 0) {
    Write-Output "HINT: no 'ui: drag apply' lines for this run -- the app was started WITHOUT LH_TRACE_DRAG=1"
}
if ($ok) { Write-Output "RESULT: PASS - the applied offset equalled the computed delta and moved the window" }
else { Write-Output "RESULT: FAIL - the window did not follow the mouse (drag delta wrong)" }
if ($ok) { exit 0 } else { exit 1 }
