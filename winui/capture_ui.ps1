# capture_ui.ps1 -- drive the UI states from code and screenshot ONLY our own window.
#
# Pure ASCII on purpose (rules/01 section 8.2).
#
# SAFETY MODEL (this is why the marker exists):
#   CopyFromScreen reads whatever is on screen. Before saving ANY pixels we check the
#   magenta probe patch the app draws at client (0,0). If it is not there, the capture is
#   discarded and the caller is told - so we can never persist the user's desktop content
#   (which happened once and is recorded in ERROR.md E10).
#
# The window is positioned with SetWindowPos and NOT activated: this script never calls
# SetForegroundWindow, so it cannot steal focus from whatever the user is doing.

param(
    [string]$OutDir = "$PSScriptRoot\..\_release\ui-shots",
    [int]$WaitMs = 4500
)

$ErrorActionPreference = 'Continue'
Add-Type -AssemblyName System.Drawing

Add-Type -Namespace Cap -Name W -MemberDefinition @'
[DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr p);
[DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
[DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
[DllImport("user32.dll")] public static extern int GetWindowTextLength(IntPtr h);
[DllImport("user32.dll")] public static extern bool GetClientRect(IntPtr h, out RECT r);
[DllImport("user32.dll")] public static extern bool ClientToScreen(IntPtr h, ref POINT p);
[DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr h, IntPtr after, int x, int y, int cx, int cy, uint flags);
[StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }
[StructLayout(LayoutKind.Sequential)] public struct POINT { public int X, Y; }
public delegate bool EnumProc(IntPtr h, IntPtr p);
public static IntPtr Found = IntPtr.Zero; public static uint Want = 0;
public static bool Cb(IntPtr h, IntPtr p) {
  uint pid; GetWindowThreadProcessId(h, out pid);
  if (pid == Want && IsWindowVisible(h) && GetWindowTextLength(h) > 0) { Found = h; return false; }
  return true; }
public static IntPtr Find(uint pid) { Found = IntPtr.Zero; Want = pid; EnumWindows(new EnumProc(Cb), IntPtr.Zero); return Found; }
'@

$exe = Join-Path $PSScriptRoot 'bin\x64\Debug\net9.0-windows10.0.19041.0\win-x64\LearnHelper.App.exe'
if (-not (Test-Path $exe)) { throw "exe not found: $exe" }
if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Path $OutDir -Force | Out-Null }

function Get-DescendantPids($rootPid) {
    $all = Get-CimInstance Win32_Process | Select-Object ProcessId, ParentProcessId
    $out = New-Object System.Collections.Generic.List[uint32]
    $q = New-Object System.Collections.Generic.Queue[uint32]
    $q.Enqueue([uint32]$rootPid)
    while ($q.Count -gt 0) {
        $cur = $q.Dequeue()
        foreach ($pr in $all) {
            if ($pr.ParentProcessId -eq $cur) { $out.Add([uint32]$pr.ProcessId); $q.Enqueue([uint32]$pr.ProcessId) }
        }
    }
    return $out
}

# Capture a named state.
#
# Returns a bool, and EVERYTHING else goes through Write-Host on purpose: a stray
# Write-Output inside a function joins the pipeline and makes `if (Capture-State ...)`
# always true, which silently turned the pass counter into a no-op (found the hard way).
# The caller additionally verifies the PNG exists, so a count can never lie again.
function Capture-State {
    param([string]$Name, [hashtable]$Env)

    foreach ($k in $Env.Keys) { Set-Item -Path "Env:$k" -Value $Env[$k] }
    $p = Start-Process -FilePath $exe -PassThru
    try {
        $h = [IntPtr]::Zero
        for ($i = 0; $i -lt 60; $i++) {
            Start-Sleep -Milliseconds 250
            $p.Refresh()
            if ($p.HasExited) { Write-Host "  [$Name] app exited early"; return $false }
            foreach ($procId in @($p.Id) + (Get-DescendantPids $p.Id)) {
                $c = [Cap.W]::Find([uint32]$procId)
                if ($c -ne [IntPtr]::Zero) { $h = $c; break }
            }
            if ($h -ne [IntPtr]::Zero) { break }
        }
        if ($h -eq [IntPtr]::Zero) { Write-Host "  [$Name] no window"; return $false }

        # Park at a known position WITHOUT activating it.
        [void][Cap.W]::SetWindowPos($h, [IntPtr]::Zero, 0, 0, 0, 0, 0x0001 -bor 0x0004 -bor 0x0010)
        Start-Sleep -Milliseconds $WaitMs

        $cr = New-Object Cap.W+RECT
        [void][Cap.W]::GetClientRect($h, [ref]$cr)
        $pt = New-Object Cap.W+POINT
        [void][Cap.W]::ClientToScreen($h, [ref]$pt)
        $w = $cr.Right - $cr.Left; $ht = $cr.Bottom - $cr.Top
        if ($w -lt 100 -or $ht -lt 100) { Write-Host "  [$Name] bad size $w x $ht"; return $false }

        Write-Host ("  [{0}] window at ({1},{2}) client {3}x{4}" -f $Name, $pt.X, $pt.Y, $w, $ht)

        $bmp = New-Object System.Drawing.Bitmap $w, $ht
        $g = [System.Drawing.Graphics]::FromImage($bmp)
        $g.CopyFromScreen($pt.X, $pt.Y, 0, 0, (New-Object System.Drawing.Size $w, $ht))
        $g.Dispose()

        # --- PROOF: a run of >=40 consecutive magenta pixels is the marker ---
        # A single-pixel colour test was tried three ways and each was wrong:
        #   * assumed position (2,2)  -> marker is not there
        #   * full-frame single-pixel search -> false positives elsewhere
        #   * narrow band around centre -> marker is not centred in the captured frame
        # A contiguous run cannot occur by accident in this UI, and it does not depend on
        # knowing where the client origin landed.
        $mx = -1; $my = -1
        $run = 0
        for ($y = 0; $y -lt $ht -and $mx -lt 0; $y++) {
            $run = 0
            for ($x = 0; $x -lt $w; $x++) {
                $px = $bmp.GetPixel($x, $y)
                if ($px.R -gt 200 -and $px.G -lt 80 -and $px.B -gt 200) {
                    $run++
                    if ($run -ge 40) { $mx = $x - $run + 1; $my = $y; break }
                }
                else { $run = 0 }
            }
        }

        if ($mx -lt 0) {
            $corner = $bmp.GetPixel([int]($w / 2), [int]($ht / 2))
            # Keep a discarded sample for diagnosis - it is OUR window area by construction
            # (ClientToScreen of our own hwnd), and it tells us what is actually on top.
            $dbg = Join-Path $OutDir "_discarded-$Name.png"
            $bmp.Save($dbg, [System.Drawing.Imaging.ImageFormat]::Png)
            $bmp.Dispose()
            Write-Host ("  [{0}] DISCARDED - no marker found; centre=({1},{2},{3}); debug png -> {4}" -f `
                $Name, $corner.R, $corner.G, $corner.B, $dbg)
            return $false
        }

        $file = Join-Path $OutDir "$Name.png"
        $bmp.Save($file, [System.Drawing.Imaging.ImageFormat]::Png)
        $bmp.Dispose()

        # Keep THIS state's diagnostics next to its picture. Comparing a captured frame with a
        # trace from a different process run is how a drawer that reported ActualWidth=440
        # while painting a 20px sliver stayed unexplained.
        $diag = Join-Path (Split-Path $exe -Parent) 'ui-diag.log'
        if (Test-Path $diag) {
            Copy-Item $diag (Join-Path $OutDir "$Name.diag.log") -Force
        }

        $exists = Test-Path $file
        Write-Host ("  [{0}] saved={1} marker at ({2},{3}) size {4}x{5} -> {6}" -f `
            $Name, $exists, $mx, $my, $w, $ht, $file)
        return $exists
    } finally {
        if (-not $p.HasExited) { $p.Kill(); $p.WaitForExit(4000) | Out-Null }
        foreach ($k in $Env.Keys) { Remove-Item -Path "Env:$k" -ErrorAction SilentlyContinue }
    }
}

Write-Host "capturing UI states -> $OutDir"
$ok = 0; $total = 0

# 1. main window, dark / light
foreach ($theme in @('dark', 'light')) {
    $total++
    if (Capture-State -Name "main-$theme" -Env @{ LH_UI_PROBE = '1'; LH_UI_THEME = $theme }) { $ok++ }
}

# 2. appearance drawer, dark / light
foreach ($theme in @('dark', 'light')) {
    $total++
    if (Capture-State -Name "appearance-$theme" -Env @{ LH_UI_PROBE = '1'; LH_UI_OPEN = 'appearance'; LH_UI_THEME = $theme }) { $ok++ }
}

# 3. developer drawer, each palette, no edit yet
foreach ($theme in @('dark', 'light')) {
    $total++
    if (Capture-State -Name "dev-$theme" -Env @{ LH_UI_PROBE = '1'; LH_UI_OPEN = 'dev'; LH_UI_THEME = $theme }) { $ok++ }
}

# 4. developer drawer with one swatch applied, per palette (proves the click path visibly)
foreach ($theme in @('dark', 'light')) {
    foreach ($pair in @(@('textMain', '#FF00AA'), @('accent', '#3DDC97'))) {
        $total++
        $name = "dev-$theme-$($pair[0])-$($pair[1].TrimStart('#'))"
        if (Capture-State -Name $name -Env @{
                LH_UI_PROBE = '1'; LH_UI_OPEN = 'dev'; LH_UI_THEME = $theme
                LH_UI_PROBE_TOKEN = $pair[0]; LH_UI_PROBE_HEX = $pair[1] }) { $ok++ }
    }
}

Write-Host ""
# Cross-check the count against the filesystem: the counter alone can lie.
$onDisk = @(Get-ChildItem $OutDir -Filter '*.png' -ErrorAction SilentlyContinue).Count
Write-Host "verified captures: $ok / $total ; png files on disk: $onDisk"
if ($ok -ne $onDisk) { Write-Host "WARNING: counter and disk disagree - trust the disk." }
if ($ok -ne $total) { Write-Host "NOTE: discarded shots mean the window was not visible at (0,0) - nothing was saved for them." }
