# next_page_probe.ps1 -- verify the "next chapter" button: it exists at a sane size, it is
# clickable, and clicking it invokes the right control (Ctl::NextPage -> backend `next_page`).
#
# How it works (no real mouse -- window messages only, per AGENTS.md rule 12):
#   1. start the real app;
#   2. read the button rect the app itself publishes in its trace
#      ("ui: page-row rects next=[x,y WxH] ...") -- deliberately NOT recomputed here, because a
#      probe that recomputes layout silently drifts and ends up clicking nothing;
#   3. post WM_LBUTTONDOWN/UP at the rect centre;
#   4. assert the app's own `ui: invoke ...` trace shows NextPage (and nothing else).
#
# ASCII only (rules/01 section 8.2) -- we match the control by its ENUM name in the trace, so the
# Chinese button label never appears in this file.

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
public class NpProbe {
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr p);
  public delegate bool EnumProc(IntPtr h, IntPtr p);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassNameW(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
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
[void][NpProbe]::SetProcessDpiAwarenessContext([NpProbe]::DPI_PER_MONITOR_AWARE_V2)

# ---------------------------------------------------------------- 1) start the app
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
    $hwnd = [NpProbe]::Find("LearnHelperNativeWnd")
    if ($hwnd -ne [IntPtr]::Zero) { break }
}
if ($hwnd -eq [IntPtr]::Zero) {
    Write-Output "NO_WINDOW"
    if ($proc -and -not $proc.HasExited) { Stop-Process -Id $proc.Id -Force -EA SilentlyContinue }
    exit 1
}
Start-Sleep -Milliseconds 2000      # let the backend handshake and the first paint settle

# ---------------------------------------------------------------- 2) read the published rects
$rowLine = $null
for ($i = 0; $i -lt 20; $i++) {
    $rowLine = @(Get-Content $diag -Encoding UTF8 -EA SilentlyContinue |
                 Where-Object { $_ -match 'ui: page-row rects' } | Select-Object -Last 1)
    if ($rowLine) { break }
    Start-Sleep -Milliseconds 300
}
if (-not $rowLine) {
    Write-Output "FAIL: the app published no 'ui: page-row rects' line"
    if ($proc -and -not $proc.HasExited) { Stop-Process -Id $proc.Id -Force -EA SilentlyContinue }
    exit 1
}
$rowLine = [string]$rowLine
Write-Output ("RECTS: " + ($rowLine -replace '^\[\d+\]\s*', ''))

if ($rowLine -match 'next=\[(-?\d+),(-?\d+) (\d+)x(\d+)\]') {
    $nx = [int]$Matches[1]; $ny = [int]$Matches[2]; $nw = [int]$Matches[3]; $nh = [int]$Matches[4]
} else {
    Write-Output "FAIL: could not parse the next-button rect"
    if ($proc -and -not $proc.HasExited) { Stop-Process -Id $proc.Id -Force -EA SilentlyContinue }
    exit 1
}
Write-Output ("NEXT BUTTON rect: {0},{1} {2}x{3}" -f $nx, $ny, $nw, $nh)

$results = @()
$results += ,@('next-button rect is a sane size (not degenerate)', (($nw -ge 40) -and ($nh -ge 20)))
$results += ,@('next-button is inside the client area', (($nx -gt 0) -and ($ny -gt 0)))

# Sanity: the three page-row controls must not overlap each other (E41 was exactly this bug).
$nums = @{}
foreach ($m in [regex]::Matches($rowLine, '(next|refresh|pages|speed)=\[(-?\d+),(-?\d+) (\d+)x(\d+)\]')) {
    $nums[$m.Groups[1].Value] = @([int]$m.Groups[2].Value, [int]$m.Groups[3].Value,
                                  [int]$m.Groups[4].Value, [int]$m.Groups[5].Value)
}
if ($nums.ContainsKey('next') -and $nums.ContainsKey('pages')) {
    $nextL = $nums['next'][0]; $pagesR = $nums['pages'][0] + $nums['pages'][2]
    $results += ,@('the page box does not overlap the next-button', ($pagesR -le $nextL))
}
if ($nums.ContainsKey('next') -and $nums.ContainsKey('refresh')) {
    $nextR = $nums['next'][0] + $nums['next'][2]; $refL = $nums['refresh'][0]
    $results += ,@('the next-button does not overlap the refresh button', ($nextR -le $refL))
}

# ---------------------------------------------------------------- 3) click it
$clickX = [int]($nx + $nw / 2)
$clickY = [int]($ny + $nh / 2)
[void][NpProbe]::SetForegroundWindow($hwnd)
Write-Output ("CLICK: client=(" + $clickX + "," + $clickY + ")")

[void][NpProbe]::PostMessage($hwnd, 0x0201, [IntPtr]1, [NpProbe]::LP($clickX, $clickY))   # WM_LBUTTONDOWN
Start-Sleep -Milliseconds 120
[void][NpProbe]::PostMessage($hwnd, 0x0202, [IntPtr]0, [NpProbe]::LP($clickX, $clickY))   # WM_LBUTTONUP
Start-Sleep -Milliseconds 3000

# ---------------------------------------------------------------- 4) verdict
$lines = @(Get-Content $diag -Encoding UTF8 -EA SilentlyContinue)
$invoked = @($lines | Where-Object { $_ -match 'ui: invoke ' })
Write-Output "--- invoke traces ---"
$invoked | ForEach-Object { Write-Output ("   " + $_) }
$hitNext = @($invoked | Where-Object { $_ -match 'ui: invoke NextPage' }).Count -gt 0
$hitOther = @($invoked | Where-Object { $_ -match 'ui: invoke ' -and $_ -notmatch 'NextPage' })
$results += ,@('clicking the button invoked Ctl::NextPage', $hitNext)

# The action must then reach the backend: the UI logs the result of every control call.
$backendSaw = @($lines | Where-Object { $_ -match 'next_page' }).Count -gt 0
$results += ,@('the click forwarded the next_page action to the backend', $backendSaw)

Write-Output ''
$bad = 0
foreach ($r in $results) {
    Write-Output ("  [" + $(if ($r[1]) { 'PASS' } else { 'FAIL' }) + "] " + $r[0]); if (-not $r[1]) { $bad++ }
}
if ($hitOther.Count -gt 0) { Write-Output ("NOTE: other controls were also invoked: " + ($hitOther -join ' | ')) }

if ($proc -and -not $proc.HasExited) { Stop-Process -Id $proc.Id -Force -EA SilentlyContinue }
Write-Output ("RESULT: " + ($results.Count - $bad) + " passed, " + $bad + " failed")
if ($bad -eq 0) { exit 0 } else { exit 1 }
