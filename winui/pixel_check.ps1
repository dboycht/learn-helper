# pixel_check.ps1 -- what colours are ACTUALLY on screen? (pure ASCII)
#
# The screenshot script already proves the frame is ours (magenta marker run). This one
# answers a different question: are the theme tokens actually applied to the SURFACES,
# not just to the text? It samples real pixels at known UI positions.
#
# Pure pixel counting: no UI automation, no synthetic input, no window activation.

$ErrorActionPreference = 'Continue'
Add-Type -AssemblyName System.Drawing

$sig = @'
using System;
using System.Runtime.InteropServices;
public class Px {
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr p);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern int GetWindowTextLength(IntPtr h);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr h, IntPtr after, int x, int y, int cx, int cy, uint f);
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }
  public delegate bool EnumProc(IntPtr h, IntPtr p);
  public static IntPtr F = IntPtr.Zero; public static uint Want = 0;
  public static bool Cb(IntPtr h, IntPtr p) { uint pid; GetWindowThreadProcessId(h, out pid); if (pid == Want && IsWindowVisible(h) && GetWindowTextLength(h) > 0) { F = h; return false; } return true; }
  public static IntPtr Find(uint pid) { F = IntPtr.Zero; Want = pid; EnumWindows(new EnumProc(Cb), IntPtr.Zero); return F; }
}
'@
Add-Type -TypeDefinition $sig -Language CSharp

$exe = 'D:\code\DeepSeekHarness\learn-helper\winui\bin\x64\Debug\net9.0-windows10.0.19041.0\win-x64\LearnHelper.App.exe'

function Sample-Theme {
    param([string]$Theme)
    $env:LH_UI_THEME = $Theme
    $env:LH_UI_PROBE = '1'
    $p = Start-Process -FilePath $exe -PassThru
    Start-Sleep -Seconds 6
    $p.Refresh()
    $h = [Px]::Find([uint32]$p.Id)
    [void][Px]::SetWindowPos($h, [IntPtr]::Zero, 0, 0, 0, 0, 0x0001 -bor 0x0004 -bor 0x0010)
    Start-Sleep -Milliseconds 1200

    $r = New-Object Px+RECT
    [void][Px]::GetWindowRect($h, [ref]$r)
    $w = $r.Right - $r.Left; $ht = $r.Bottom - $r.Top

    $bmp = New-Object System.Drawing.Bitmap $w, $ht
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.CopyFromScreen($r.Left, $r.Top, 0, 0, (New-Object System.Drawing.Size $w, $ht))
    $g.Dispose()
    $dst = "D:\code\DeepSeekHarness\learn-helper\_release\px-$Theme.png"
    $bmp.Save($dst, [System.Drawing.Imaging.ImageFormat]::Png)

    # Marker run check (same proof as capture_ui.ps1).
    $found = $false
    for ($y = 0; $y -lt 40 -and -not $found; $y++) {
        $run = 0
        for ($x = 0; $x -lt $w; $x++) {
            $px = $bmp.GetPixel($x, $y)
            if ($px.R -gt 200 -and $px.G -lt 80 -and $px.B -gt 200) { $run++; if ($run -ge 40) { $found = $true; break } } else { $run = 0 }
        }
    }

    Write-Host "--- theme=$Theme  frame=${w}x${ht}  marker=$found ---"
    if (-not $found) { Write-Host "    discarded (not our window)"; $bmp.Dispose(); Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue; return }

    # Sample points relative to the captured frame, reasoned from the layout:
    #   y=200 -> "runtime status" card body        y=340 -> KPI card
    #   y=640 -> log area                          y=95  -> title bar
    foreach ($pt in @(@(600, 200, 'runtime card'), @(400, 340, 'KPI card'), @(600, 640, 'log area'), @(600, 20, 'title bar'), @(600, 95, 'notice bar'))) {
        $c = $bmp.GetPixel([int]$pt[0], [int]$pt[1])
        $lum = [math]::Round(0.2126 * $c.R + 0.7152 * $c.G + 0.0722 * $c.B)
        Write-Host ("    {0,-14} rgb=({1,3},{2,3},{3,3})  lum={4,3}" -f $pt[2], $c.R, $c.G, $c.B, $lum)
    }
    $bmp.Dispose()
    if (-not $p.HasExited) { $p.Kill(); $p.WaitForExit(3000) | Out-Null }
}

Sample-Theme -Theme 'dark'
Sample-Theme -Theme 'light'
Remove-Item Env:LH_UI_THEME, Env:LH_UI_PROBE -ErrorAction SilentlyContinue
Write-Host ""
Write-Host "EXPECTED: dark theme -> every surface lum LOW (<70); light theme -> every surface lum HIGH (>200)"
