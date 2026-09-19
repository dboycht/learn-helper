# autolaunch_pause_probe.ps1 -- the auto browser launch must NOT close what the user just opened.
#
# Why this exists: the sandbox browser takes focus. Our own popups (the "current page" dropdown,
# the settings dialog, the about box) close themselves on WM_ACTIVATE(WA_INACTIVE) -- that is the
# intended behaviour for "click elsewhere". So when the auto-launch fired while a dropdown was open,
# the dropdown vanished under the user's fingers (and `pages_probe` failed intermittently when run
# straight after another probe). Since 2.1.3 the launch waits until no own-popup is open first.
#
# What it proves (nobody clicks anything):
#   1) the launch is armed at startup, but the dropdown opened first stays alive well past the
#      launch delay ("waiting for dialogs" is visible in the diagnostics)
#   2) the launch still happens afterwards: once the dropdown is closed, the browser is started
#
# ASCII only (PS 5.1 reads BOM-less scripts as GBK).

param(
    [string]$Exe = "D:\code\DeepSeekHarness\learn-helper\native\target\release\learn-helper-native.exe",
    [int]$WaitSeconds = 45
)

$ErrorActionPreference = 'Stop'
$script:pass = 0
$script:fail = 0
$script:diag = Join-Path (Split-Path -Parent $Exe) 'native-diag.log'

function Check([string]$name, [bool]$ok, [string]$detail = '') {
    if ($ok) { $script:pass++; Write-Host ("  [PASS] " + $name + $(if ($detail) { " -- " + $detail } else { '' })) }
    else { $script:fail++; Write-Host ("  [FAIL] " + $name + $(if ($detail) { " -- " + $detail } else { '' })) }
}

Add-Type -TypeDefinition @'
using System;
using System.Text;
using System.Runtime.InteropServices;
public class AlpProbe {
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr p);
  public delegate bool EnumProc(IntPtr h, IntPtr p);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassNameW(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern bool IsWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern IntPtr SendMessage(IntPtr h, uint m, IntPtr w, IntPtr l);
  public static IntPtr Find(string cls) {
    IntPtr f = IntPtr.Zero;
    EnumWindows((h,p) => { var sb=new StringBuilder(256); GetClassNameW(h,sb,256);
      if (sb.ToString()==cls) { f=h; return false; } return true; }, IntPtr.Zero);
    return f;
  }
  // Close a popup the way the app itself does (WM_CLOSE), not by killing anything.
  public static void Close(IntPtr h) { if (h != IntPtr.Zero) { SendMessage(h, 0x0010, IntPtr.Zero, IntPtr.Zero); } }
}
'@

function Port-Open() {
    try {
        $c = New-Object System.Net.Sockets.TcpClient
        $iar = $c.BeginConnect('127.0.0.1', 9222, $null, $null)
        $ok = $iar.AsyncWaitHandle.WaitOne(400, $false)
        if ($ok) { $c.EndConnect($iar) }
        $c.Close()
        return $ok
    } catch { return $false }
}

function Diag-Lines() {
    if (Test-Path $script:diag) { return @(Get-Content $script:diag -Encoding UTF8 -ErrorAction SilentlyContinue) }
    return @()
}

Write-Host "=== auto browser launch must not close an open popup ==="
if (-not (Test-Path $Exe)) { Write-Host "EXE not found"; exit 2 }
if (@(Get-Process -Name 'learn-helper-native' -ErrorAction SilentlyContinue).Count -gt 0) {
    Write-Host "ABORT: learn-helper-native already running (single-instance mutex)"; exit 3
}

$dir = Join-Path $env:TEMP ("lh-alpause-" + (Get-Random))
New-Item -ItemType Directory -Path $dir -Force | Out-Null
$cfg = @{ server_url='http://127.0.0.1:9'; answer=@{mode='server'}; run=@{ auto_launch_browser=$true } } | ConvertTo-Json -Depth 5
[IO.File]::WriteAllText((Join-Path $dir 'config.json'), $cfg, (New-Object System.Text.UTF8Encoding($false)))

$env:LH_BASE_DIR = $dir
    # NOTE: do NOT set LH_NO_AUTO_BROWSER here. This probe's WHOLE POINT is that the app comes
    # back 4s later and decides to give way to the open dialog; suppressing the feature would
    # make the probe unable to fail (the same mistake as the tautology in autolaunch_probe).
    # The app still must not really open a browser -- that is what the "skipped" assertion checks.
$env:LH_PROBE_PAGES = "page one|page two|page three"
$env:LH_UI_ACTION = 'pages'
# Keep the dropdown's own hook teardown out of the way: it would close the main window after 1.8s,
# which is exactly the window we need to observe (see pagepicker::hook_delay_ms).
$env:LH_KEEP_OPEN = '1'
Remove-Item $script:diag -Force -ErrorAction SilentlyContinue
$p = Start-Process -FilePath $Exe -WorkingDirectory $dir -PassThru

# Wait for our dropdown to open (the hook opens it ~3.5s in; the picker then keeps hook mode alive).
$picker = [IntPtr]::Zero
$deadline = (Get-Date).AddSeconds(25)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 250
    if ($p.HasExited) { break }
    $picker = [AlpProbe]::Find("LearnHelperPagePickerWnd")
    if ($picker -ne [IntPtr]::Zero) { break }
}
# The launch is armed 4s after startup. It must REFUSE to fire while our dropdown is open,
# because starting the browser steals focus and the dropdown closes itself on focus loss.
Check "page dropdown opened before the launch delay" ($picker -ne [IntPtr]::Zero)
$sawSkip = $false
$deadline = (Get-Date).AddSeconds(12)
while ((Get-Date) -lt $deadline) {
    $lines = Diag-Lines
    if (@($lines | Where-Object { $_ -match 'auto launch skipped \(dialog open\)' }).Count -gt 0) { $sawSkip = $true; break }
    Start-Sleep -Milliseconds 400
}
Check "diagnostics show the auto launch giving way to the open dialog" $sawSkip
Check "dropdown still alive (nothing stole its focus)" (($picker -ne [IntPtr]::Zero) -and [AlpProbe]::IsWindow($picker))
$lines = Diag-Lines
Check "no browser was launched while the dropdown is open" (@($lines | Where-Object { $_ -match 'launch_browser -> ' }).Count -eq 0)

# Close the dropdown the way the app does. The deferred launch was given up on purpose, so nothing
# should fire now either -- and the user can always click "refresh pages" when they want the browser.
[AlpProbe]::Close($picker)
Start-Sleep -Seconds 4
$lines2 = Diag-Lines
Check "the skipped launch stayed skipped (user opens it manually instead)" (@($lines2 | Where-Object { $_ -match 'launch_browser -> ' }).Count -eq 0)

if (-not $p.HasExited) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 3
Remove-Item Env:LH_BASE_DIR, Env:LH_PROBE_PAGES, Env:LH_UI_ACTION, Env:LH_KEEP_OPEN -ErrorAction SilentlyContinue

# The browser we just caused to start is shut down by the app on exit; verify nothing lingers.
$portAfter = Port-Open
Check "sandbox browser cleaned up after the app exited" (-not $portAfter) ("port open = " + $portAfter)
$left = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -like '*learn-helper*' })
Check "backend process cleaned up" ($left.Count -eq 0) ("left=" + $left.Count)

Write-Host ""
Write-Host ("RESULT: " + $script:pass + " passed, " + $script:fail + " failed")
Write-Host ("temp dir kept for inspection: " + $dir)
if ($script:fail -gt 0) { exit 1 }
exit 0
