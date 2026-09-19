# icon_probe.ps1 -- the exe must carry the multi-size application icon, and it must render.
#
# Why: the icon is stamped into the already-built exe (see native/build_release.ps1 and
# _tools/make-icon.ps1). A rebuild wipes the resources, so this probe is the regression check that
# the release pipeline did its job. It needs no window, no mouse, nothing on screen.
#
# What it proves:
#   1) RT_GROUP_ICON (id 1) + RT_ICON frames 1..7 exist in the exe (16/24/32/48/64/128/256)
#   2) the frames actually render: the 32px frame has the icon's blue background, white robot body
#      and its yellow bulb -- i.e. it is not a blank/black silently-broken bitmap
#
# ASCII only (PS 5.1 reads BOM-less scripts as GBK).

param(
    [string]$Exe = "D:\code\DeepSeekHarness\learn-helper\native\target\release\learn-helper-native.exe",
    [int]$ExpectedFrames = 7
)

$ErrorActionPreference = 'Stop'
$script:pass = 0
$script:fail = 0

function Check([string]$name, [bool]$ok, [string]$detail = '') {
    if ($ok) { $script:pass++; Write-Host ("  [PASS] " + $name + $(if ($detail) { " -- " + $detail } else { '' })) }
    else { $script:fail++; Write-Host ("  [FAIL] " + $name + $(if ($detail) { " -- " + $detail } else { '' })) }
}

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public class IconProbe {
  [DllImport("kernel32.dll", SetLastError=true, CharSet=CharSet.Unicode)]
  public static extern IntPtr LoadLibraryEx(string f, IntPtr h, uint flags);
  [DllImport("kernel32.dll")] public static extern bool FreeLibrary(IntPtr h);
  [DllImport("kernel32.dll", SetLastError=true)]
  public static extern bool EnumResourceNames(IntPtr h, IntPtr type, EnumProc cb, IntPtr p);
  public delegate bool EnumProc(IntPtr h, IntPtr type, IntPtr name, IntPtr p);
  public static readonly IntPtr RT_ICON = new IntPtr(3);
  public static readonly IntPtr RT_GROUP_ICON = new IntPtr(14);
  public static int Count(IntPtr h, IntPtr type) {
    int n = 0;
    EnumResourceNames(h, type, (hh,t,name,p) => { n++; return true; }, IntPtr.Zero);
    return n;
  }
}
'@
Add-Type -AssemblyName System.Drawing

Write-Host "=== application icon probe ==="
Write-Host ("exe = " + $Exe)
if (-not (Test-Path $Exe)) { Write-Host "EXE not found"; exit 2 }

# 1) resource structure
$h = [IconProbe]::LoadLibraryEx($Exe, [IntPtr]::Zero, 0x00000002)   # LOAD_LIBRARY_AS_DATAFILE
Check "exe can be opened as a data file" ($h -ne [IntPtr]::Zero)
$groups = [IconProbe]::Count($h, [IconProbe]::RT_GROUP_ICON)
$frames = [IconProbe]::Count($h, [IconProbe]::RT_ICON)
[void][IconProbe]::FreeLibrary($h)
Check "RT_GROUP_ICON present (the icon the shell shows)" ($groups -ge 1) ("groups=" + $groups)
Check ("RT_ICON has " + $ExpectedFrames + " frames (16/24/32/48/64/128/256)") ($frames -eq $ExpectedFrames) ("frames=" + $frames)

# 2) it renders: pull the 32px frame out of the exe and look at real pixels
$icon = [System.Drawing.Icon]::ExtractAssociatedIcon($Exe)
Check "shell can extract an icon from the exe" ($null -ne $icon)
if ($null -eq $icon) {
    Write-Host ""
    Write-Host ("RESULT: " + $script:pass + " passed, " + $script:fail + " failed")
    exit 1
}
Check "extracted frame is 32x32 (the taskbar size)" (($icon.Width -eq 32) -and ($icon.Height -eq 32)) ($icon.Width.ToString() + "x" + $icon.Height)

$bmp = $icon.ToBitmap()
function Px([System.Drawing.Bitmap]$b, [int]$x, [int]$y) {
    $c = $b.GetPixel($x, $y)
    return ("#{0:X2}{1:X2}{2:X2}" -f $c.R, $c.G, $c.B)
}
$corner = Px $bmp 0 0            # background
$body = Px $bmp 16 20            # robot body / book
$bulb = Px $bmp 25 5             # the yellow bulb top-right
Write-Host ("  sampled 32px frame: corner=" + $corner + " body=" + $body + " bulb=" + $bulb)

function Blue([string]$hex) {
    $r = [Convert]::ToInt32($hex.Substring(1, 2), 16)
    $g = [Convert]::ToInt32($hex.Substring(3, 2), 16)
    $b = [Convert]::ToInt32($hex.Substring(5, 2), 16)
    return (($b -gt 150) -and ($b -gt ($r + 40)) -and ($b -gt ($g + 20)))
}
function Light([string]$hex) {
    # The artwork is blue-tinted: the "white" robot reads as #BAD4FE (r=186), so a plain
    # "r,g,b > 200" test is wrong. Use overall brightness instead.
    $r = [Convert]::ToInt32($hex.Substring(1, 2), 16)
    $g = [Convert]::ToInt32($hex.Substring(3, 2), 16)
    $b = [Convert]::ToInt32($hex.Substring(5, 2), 16)
    return ((($r + $g + $b) -gt 520) -and ($b -gt 150))
}
function Yellow([string]$hex) {
    $r = [Convert]::ToInt32($hex.Substring(1, 2), 16)
    $g = [Convert]::ToInt32($hex.Substring(3, 2), 16)
    $b = [Convert]::ToInt32($hex.Substring(5, 2), 16)
    return (($r -gt 190) -and ($g -gt 150) -and ($b -lt 130))
}

Check "corner is the icon's blue background" (Blue $corner) ("got " + $corner)
Check "the robot body pixel is light (white/blue-grey)" (Light $body) ("got " + $body)
Check "the bulb pixel is yellow" (Yellow $bulb) ("got " + $bulb)
Check "the frame is not blank (body differs from corner)" ($body -ne $corner) ($body + " vs " + $corner)

$bmp.Dispose()
$icon.Dispose()

Write-Host ""
Write-Host ("RESULT: " + $script:pass + " passed, " + $script:fail + " failed")
if ($script:fail -gt 0) { exit 1 }
exit 0
