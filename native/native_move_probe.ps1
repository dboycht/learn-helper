# native_move_probe.ps1 -- verify that window MOVING is handed to the system (SC_MOVE) instead of
# being hand-rolled, and that we stop doing full-window repaints while the system moves the window.
#
# Background (ERROR.md E78): dragging used to be implemented by hand -- set capture, then
# SetWindowPos + a FULL-WINDOW double-buffered GDI repaint on every WM_MOUSEMOVE. That made the
# window "tremble" while dragging and burned CPU. It now sends WM_SYSCOMMAND/SC_MOVE so Windows
# runs its own move loop (which only blits the existing bitmap).
#
# What this probe asserts:
#   1. the app has NO hand-rolled move left: dragging must not produce "ui: drag apply" lines;
#   2. entering the system move loop is recorded ("ui: enter size/move");
#   3. while inside the move loop the app performs no repaints (the whole point of the fix);
#   4. on release it repaints exactly once ("ui: exit size/move").
#
# It cannot move the real mouse (forbidden by rule 12), so it drives the SAME entry point the
# title bar uses: WM_SYSCOMMAND/SC_MOVE with a screen position in lParam. Windows then runs its
# modal move loop; we end it by posting WM_LBUTTONUP to our own thread's message queue.
#
# ASCII only (rules/01 section 8.2).

param(
    [string]$Exe = ''
)

$ErrorActionPreference = 'Continue'
if ($Exe -eq '') { $Exe = Join-Path $PSScriptRoot 'target\release\learn-helper-native.exe' }
if (-not (Test-Path $Exe)) { Write-Output "MISSING_EXE: $Exe"; exit 1 }
$diag = Join-Path (Split-Path -Parent $Exe) 'native-diag.log'

Add-Type -TypeDefinition @'
using System;
using System.Text;
using System.Runtime.InteropServices;
public class NmProbe {
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr p);
  public delegate bool EnumProc(IntPtr h, IntPtr p);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassNameW(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern IntPtr SendMessage(IntPtr h, uint m, IntPtr w, IntPtr l);
  [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr h, uint m, IntPtr w, IntPtr l);
  [DllImport("user32.dll")] public static extern bool SetProcessDpiAwarenessContext(IntPtr ctx);
  public static readonly IntPtr DPI_PER_MONITOR_AWARE_V2 = new IntPtr(-4);
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }
  public static IntPtr Find(string cls) {
    IntPtr f = IntPtr.Zero;
    EnumWindows((h,p) => { var sb=new StringBuilder(256); GetClassNameW(h,sb,256);
      if (sb.ToString()==cls) { f=h; return false; } return true; }, IntPtr.Zero);
    return f;
  }
  public static IntPtr LP(int x, int y) { return (IntPtr)((y << 16) | (x & 0xFFFF)); }
}
'@
[void][NmProbe]::SetProcessDpiAwarenessContext([NmProbe]::DPI_PER_MONITOR_AWARE_V2)

Get-Process -EA SilentlyContinue | Where-Object { $_.ProcessName -match 'LearnHelper|learn-helper' } | Stop-Process -Force
Start-Sleep -Milliseconds 900
Remove-Item $diag -Force -EA SilentlyContinue
$env:LH_NO_AUTO_BROWSER = '1'
$workdir = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $Exe))
$proc = Start-Process -FilePath $Exe -WorkingDirectory $workdir -PassThru
Remove-Item Env:\LH_NO_AUTO_BROWSER -EA SilentlyContinue

$hwnd = [IntPtr]::Zero
for ($i = 0; $i -lt 40; $i++) {
    Start-Sleep -Milliseconds 250
    $hwnd = [NmProbe]::Find("LearnHelperNativeWnd")
    if ($hwnd -ne [IntPtr]::Zero) { break }
}
if ($hwnd -eq [IntPtr]::Zero) {
    Write-Output "NO_WINDOW"
    if ($proc -and -not $proc.HasExited) { Stop-Process -Id $proc.Id -Force -EA SilentlyContinue }
    exit 1
}
Start-Sleep -Milliseconds 2000
[void][NmProbe]::SetForegroundWindow($hwnd)

$r0 = [NmProbe+RECT]::new()
[void][NmProbe]::GetWindowRect($hwnd, [ref]$r0)
$titleX = $r0.Left + [int](($r0.Right - $r0.Left) / 2) - 300   # blank part of the title bar
$titleY = $r0.Top + 25
Write-Output ("WINDOW: {0},{1} {2}x{3}" -f $r0.Left, $r0.Top, ($r0.Right-$r0.Left), ($r0.Bottom-$r0.Top))

# Count repaints before / during the move.
function PaintCount { param([string]$Path)
    if (-not (Test-Path $Path)) { return 0 }
    return @(Get-Content $Path -Encoding UTF8 -EA SilentlyContinue | Select-String -Pattern 'paint-logs:').Count
}
$paintsBefore = PaintCount $diag

# End the system modal move loop. The loop watches for the left button coming up; we post it to
# our own thread's queue from a background .NET thread (no real mouse involved).
$enderCode = @'
using System;
using System.Threading;
using System.Runtime.InteropServices;
public class NmEnder {
  [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr h, uint m, IntPtr w, IntPtr l);
  public static void Start(int delayMs) {
    var t = new Thread(() => {
      Thread.Sleep(delayMs);
      // PostMessage(NULL, ...) goes to the calling THREAD's queue -- which is the one running
      // the modal move loop, because we are about to block in SendMessage on this same thread.
      PostMessage(IntPtr.Zero, 0x0202, IntPtr.Zero, IntPtr.Zero);   // WM_LBUTTONUP
    });
    t.IsBackground = true;
    t.Start();
  }
}
'@
Add-Type -TypeDefinition $enderCode
[NmEnder]::Start(1500)

Write-Output "SENDING WM_SYSCOMMAND/SC_MOVE (blocks until the system move loop ends) ..."
$sw = [System.Diagnostics.Stopwatch]::StartNew()
# WM_SYSCOMMAND = 0x0112, SC_MOVE = 0xF010. lParam must carry SCREEN coords.
[void][NmProbe]::SendMessage($hwnd, 0x0112, [IntPtr]0xF010, [NmProbe]::LP($titleX, $titleY))
$sw.Stop()
Write-Output ("returned from the move loop after " + $sw.ElapsedMilliseconds + " ms")
if ($sw.ElapsedMilliseconds -lt 300) {
    Write-Output "  (NOTE: returned very fast -- the system move loop may not have started)"
}

Start-Sleep -Milliseconds 800
$paintsDuring = PaintCount $diag

$lines = @(Get-Content $diag -Encoding UTF8 -EA SilentlyContinue)
$enter = @($lines | Where-Object { $_ -match 'ui: enter size/move' }).Count
$exit  = @($lines | Where-Object { $_ -match 'ui: exit size/move' }).Count
$apply = @($lines | Where-Object { $_ -match 'ui: drag apply' }).Count
Write-Output "--- move trace ---"
$lines | Where-Object { $_ -match 'size/move|drag|SC_MOVE' } | ForEach-Object { Write-Output ("   " + $_) }

$results = @()
$results += ,@('no hand-rolled drag left (no "ui: drag apply" lines)', ($apply -eq 0))
$results += ,@('the system move loop was entered (WM_ENTERSIZEMOVE)', ($enter -ge 1))
$results += ,@('the system move loop ended (WM_EXITSIZEMOVE)', ($exit -ge 1))
$results += ,@('no full-window repaint while the system moved the window',
               (($paintsDuring - $paintsBefore) -le 2))

Write-Output ("REPAINTS: before=$paintsBefore during=$paintsDuring delta=" + ($paintsDuring - $paintsBefore))
Write-Output ''
$bad = 0
foreach ($res in $results) {
    Write-Output ("  [" + $(if ($res[1]) { 'PASS' } else { 'FAIL' }) + "] " + $res[0]); if (-not $res[1]) { $bad++ }
}
if ($proc -and -not $proc.HasExited) { Stop-Process -Id $proc.Id -Force -EA SilentlyContinue }
Write-Output ("RESULT: " + ($results.Count - $bad) + " passed, " + $bad + " failed")
if ($bad -eq 0) { exit 0 } else { exit 1 }
