# pixel_check_native.ps1 -- native UI rendering check (pure ASCII + Chinese data kept in
# the process only, so PS 5.1 encoding rules are respected).
#
# Prints the window rect, a few sampled pixel colours and a coarse brightness map.
# Use it to answer "did the window actually render, and did the glass/dark theme apply?"
# without looking at the screen.
#
# Usage: powershell -ExecutionPolicy Bypass -File pixel_check_native.ps1

Add-Type -AssemblyName System.Drawing

$code = @'
using System;
using System.Text;
using System.Collections.Generic;
using System.Runtime.InteropServices;
public class WinCap {
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr h, IntPtr dc, uint flags);
  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int cmd);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr p);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassNameW(IntPtr h, StringBuilder s, int n);
  public delegate bool EnumProc(IntPtr h, IntPtr p);
  [StructLayout(LayoutKind.Sequential)]
  public struct RECT { public int Left, Top, Right, Bottom; }

  // FindWindowW(cls, NULL) proved unreliable here, so enumerate by class name instead.
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

$hwnd = [WinCap]::FindByClass("LearnHelperNativeWnd")
if ($hwnd -eq [IntPtr]::Zero) { Write-Output "RESULT: NO_WINDOW"; exit 1 }
if ([WinCap]::IsIconic($hwnd)) { [void][WinCap]::ShowWindow($hwnd, 9) ; Start-Sleep -Milliseconds 400 }
[void][WinCap]::SetForegroundWindow($hwnd)
Start-Sleep -Milliseconds 500

$r = New-Object WinCap+RECT
[void][WinCap]::GetWindowRect($hwnd, [ref]$r)
$w = $r.Right - $r.Left
$h = $r.Bottom - $r.Top
Write-Output ("WINDOW: {0}x{1} at {2},{3}" -f $w, $h, $r.Left, $r.Top)

$bmp = New-Object System.Drawing.Bitmap($w, $h)
$g = [System.Drawing.Graphics]::FromImage($bmp)
$hdc = $g.GetHdc()
$ok = [WinCap]::PrintWindow($hwnd, $hdc, 0)
$g.ReleaseHdc($hdc)
$g.Dispose()
Write-Output ("PRINTWINDOW: {0}" -f $ok)

$path = Join-Path $PSScriptRoot "native-capture.png"
$bmp.Save($path, [System.Drawing.Imaging.ImageFormat]::Png)
Write-Output ("SAVED: {0}" -f $path)

function Sample($x, $y) {
  $c = $bmp.GetPixel($x, $y)
  return ("{0},{1} = #{2:X2}{3:X2}{4:X2}" -f $x, $y, $c.R, $c.G, $c.B)
}

# Coarse brightness map: 6x6 grid of average luminance (0-255)
Write-Output "LUMINANCE MAP (6x6):"
for ($gy = 0; $gy -lt 6; $gy++) {
  $row = ""
  for ($gx = 0; $gx -lt 6; $gx++) {
    $sum = 0; $n = 0
    for ($y = [int]($gy * $h / 6); $y -lt [int](($gy + 1) * $h / 6); $y += 8) {
      for ($x = [int]($gx * $w / 6); $x -lt [int](($gx + 1) * $w / 6); $x += 8) {
        $c = $bmp.GetPixel($x, $y)
        $sum += (0.299 * $c.R + 0.587 * $c.G + 0.114 * $c.B); $n++
      }
    }
    if ($n -gt 0) { $row += ("{0,4}" -f [int]($sum / $n)) } else { $row += "   -" }
  }
  Write-Output $row
}

# Real window width may include shadow; keep samples inside.
$cx = [int]($w / 2); $cy = [int]($h / 2)
Write-Output "SAMPLES:"
Sample 6 6
Sample ($w - 6) 6
Sample $cx 20
Sample $cx $cy
Sample 40 ($h - 40)
Sample ($w - 40) ($h - 40)

$bmp.Dispose()
Write-Output "RESULT: OK"
