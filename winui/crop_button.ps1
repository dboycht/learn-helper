# crop_button.ps1 -- crop the primary button area from a fresh capture and zoom it (pure ASCII)
#
# Why: full-frame pixel SAMPLING kept reading whatever window was on top. Cropping a small
# region and looking at it directly is checkable by eye, and the magenta marker still proves
# the frame belongs to our window before anything is saved.

$ErrorActionPreference = 'Continue'
Add-Type -AssemblyName System.Drawing

$sig = @'
using System; using System.Runtime.InteropServices;
public class Cb2 {
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr p);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern int GetWindowTextLength(IntPtr h);
  [DllImport("user32.dll")] public static extern bool GetClientRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool ClientToScreen(IntPtr h, ref POINT p);
  [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr h, IntPtr a, int x, int y, int cx, int cy, uint f);
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }
  [StructLayout(LayoutKind.Sequential)] public struct POINT { public int X, Y; }
  public delegate bool EnumProc(IntPtr h, IntPtr p);
  public static IntPtr F = IntPtr.Zero; public static uint Want = 0;
  public static bool Cb(IntPtr h, IntPtr p){ uint pid; GetWindowThreadProcessId(h, out pid); if(pid==Want && IsWindowVisible(h) && GetWindowTextLength(h)>0){F=h; return false;} return true; }
  public static IntPtr Find(uint pid){ F=IntPtr.Zero; Want=pid; EnumWindows(new EnumProc(Cb), IntPtr.Zero); return F; }
}
'@
Add-Type -TypeDefinition $sig -Language CSharp

$exe = 'D:\code\DeepSeekHarness\learn-helper\winui\bin\x64\Debug\net9.0-windows10.0.19041.0\win-x64\LearnHelper.App.exe'
$outDir = 'D:\code\DeepSeekHarness\learn-helper\_release\crops'
if (-not (Test-Path $outDir)) { New-Item -ItemType Directory -Path $outDir -Force | Out-Null }

foreach ($theme in @('dark', 'light')) {
    $env:LH_UI_PROBE = '1'
    $env:LH_UI_THEME = $theme
    $p = Start-Process -FilePath $exe -PassThru
    Start-Sleep -Seconds 6
    $p.Refresh()
    $h = [Cb2]::Find([uint32]$p.Id)
    [void][Cb2]::SetWindowPos($h, [IntPtr]::Zero, 0, 0, 0, 0, 0x0001 -bor 0x0004 -bor 0x0010)
    Start-Sleep -Milliseconds 1500

    $cr = New-Object Cb2+RECT; [void][Cb2]::GetClientRect($h, [ref]$cr)
    $pt = New-Object Cb2+POINT; [void][Cb2]::ClientToScreen($h, [ref]$pt)
    $w = $cr.Right - $cr.Left; $ht = $cr.Bottom - $cr.Top

    $bmp = New-Object System.Drawing.Bitmap $w, $ht
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.CopyFromScreen($pt.X, $pt.Y, 0, 0, (New-Object System.Drawing.Size $w, $ht))
    $g.Dispose()

    # Marker run check.
    $ours = $false
    for ($y = 0; $y -lt 40 -and -not $ours; $y++) {
        $run = 0
        for ($x = 0; $x -lt $w; $x++) {
            $px = $bmp.GetPixel($x, $y)
            if ($px.R -gt 200 -and $px.G -lt 80 -and $px.B -gt 200) { $run++; if ($run -ge 40) { $ours = $true; break } } else { $run = 0 }
        }
    }

    if ($ours) {
        # The controls row sits just above the log card; crop generously and zoom 3x.
        $cropY = [int]($ht * 0.70)
        $rect2 = New-Object System.Drawing.Rectangle 20, $cropY, 620, 70
        $crop = $bmp.Clone($rect2, $bmp.PixelFormat)
        $zoom = New-Object System.Drawing.Bitmap ($crop.Width * 3), ($crop.Height * 3)
        $gz = [System.Drawing.Graphics]::FromImage($zoom)
        $gz.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::NearestNeighbor
        $gz.DrawImage($crop, 0, 0, $zoom.Width, $zoom.Height)
        $gz.Dispose()
        $file = Join-Path $outDir "buttons-$theme.png"
        $zoom.Save($file, [System.Drawing.Imaging.ImageFormat]::Png)
        $zoom.Dispose(); $crop.Dispose()
        Write-Host "  [$theme] saved $file"
    }
    else {
        Write-Host "  [$theme] DISCARDED (marker not found - window was covered)"
    }

    $bmp.Dispose()
    if (-not $p.HasExited) { $p.Kill(); $p.WaitForExit(3000) | Out-Null }
    Remove-Item Env:LH_UI_PROBE, Env:LH_UI_THEME -ErrorAction SilentlyContinue
}
