# probe_layout.ps1 -- capture the window AND report the exact geometry + a fine-grained
# scan of the KPI tiles so a layout defect is measurable, not just visible.
# ASCII only (rules/01 section 8.2).

Add-Type -AssemblyName System.Drawing

$code = @'
using System;
using System.Text;
using System.Collections.Generic;
using System.Runtime.InteropServices;
public class LProbe {
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool GetClientRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr h, IntPtr dc, uint flags);
  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int cmd);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr p);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassNameW(IntPtr h, StringBuilder s, int n);
  public delegate bool EnumProc(IntPtr h, IntPtr p);
  [StructLayout(LayoutKind.Sequential)]
  public struct RECT { public int Left, Top, Right, Bottom; }
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
}
'@
Add-Type -TypeDefinition $code

$hwnd = [LProbe]::FindByClass("LearnHelperNativeWnd")
if ($hwnd -eq [IntPtr]::Zero) { Write-Output "NO_WINDOW"; exit 1 }
if ([LProbe]::IsIconic($hwnd)) { [void][LProbe]::ShowWindow($hwnd, 9); Start-Sleep -Milliseconds 400 }
[void][LProbe]::SetForegroundWindow($hwnd)
Start-Sleep -Milliseconds 600

$wr = New-Object LProbe+RECT; [void][LProbe]::GetWindowRect($hwnd, [ref]$wr)
$cr = New-Object LProbe+RECT; [void][LProbe]::GetClientRect($hwnd, [ref]$cr)
$ww = $wr.Right - $wr.Left; $wh = $wr.Bottom - $wr.Top
$cw = $cr.Right - $cr.Left; $ch = $cr.Bottom - $cr.Top
Write-Output ("WINDOW_RECT : {0}x{1} at {2},{3}" -f $ww, $wh, $wr.Left, $wr.Top)
Write-Output ("CLIENT_RECT : {0}x{1}" -f $cw, $ch)
Write-Output ("NONCLIENT   : dx={0} dy={1}" -f ($ww - $cw), ($wh - $ch))

$bmp = New-Object System.Drawing.Bitmap($ww, $wh)
$g = [System.Drawing.Graphics]::FromImage($bmp)
$hdc = $g.GetHdc(); [void][LProbe]::PrintWindow($hwnd, $hdc, 0); $g.ReleaseHdc($hdc); $g.Dispose()
$path = Join-Path $PSScriptRoot "native-capture.png"
$bmp.Save($path, [System.Drawing.Imaging.ImageFormat]::Png)
Write-Output ("SAVED       : {0} ({1}x{2})" -f $path, $bmp.Width, $bmp.Height)

# Horizontal band scan at the KPI row: report rows that contain "ink" (non-background pixels)
# so text/number bands show up as measurable ranges.
function RowInk($y) {
  $n = 0
  for ($x = 20; $x -lt ($ww - 20); $x += 2) {
    $c = $bmp.GetPixel($x, $y)
    # background tokens are #101418 (16,20,24) and card #171C22 (23,28,34)
    $isBg = ([Math]::Abs($c.R - 16) -lt 6 -and [Math]::Abs($c.G - 20) -lt 6 -and [Math]::Abs($c.B - 24) -lt 6) -or
            ([Math]::Abs($c.R - 23) -lt 6 -and [Math]::Abs($c.G - 28) -lt 6 -and [Math]::Abs($c.B - 34) -lt 6) -or
            ([Math]::Abs($c.R - 30) -lt 8 -and [Math]::Abs($c.G - 36) -lt 8 -and [Math]::Abs($c.B - 44) -lt 8)
    if (-not $isBg) { $n++ }
  }
  return $n
}

Write-Output "INK ROWS (y : ink-count) -- find the KPI number/label bands:"
$bands = @()
$inBand = $false; $start = 0
for ($y = 0; $y -lt $wh; $y += 1) {
  $ink = RowInk $y
  if ($ink -gt 3 -and -not $inBand) { $inBand = $true; $start = $y }
  elseif ($ink -le 3 -and $inBand) {
    $inBand = $false
    $bands += ("{0}-{1}" -f $start, ($y - 1))
  }
}
if ($inBand) { $bands += ("{0}-{1}" -f $start, ($wh - 1)) }
Write-Output ($bands -join "  ")
$bmp.Dispose()
