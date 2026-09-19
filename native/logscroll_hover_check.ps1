# logscroll_hover_check.ps1 -- mouse-free proof that the log scroll bar highlights on hover.
#
# The end-to-end probe cannot assert hover from a dump: it drives the UI with posted messages only
# (it must never hijack the user's real pointer), while the OS keeps posting genuine WM_MOUSEMOVE
# messages at wherever the physical cursor is -- and those overwrite the hover flag between our
# message and the repaint. So the highlight is verified where it is fully deterministic instead:
#
#   1) render the window with the pointer pinned OFF the bar (LH_PROBE_HOVER_BAR=0) -> thumb uses
#      the muted colour (#8B96A5 dark theme)
#   2) render it again with the pointer pinned ON it (LH_PROBE_HOVER_BAR=1)     -> thumb uses the
#      accent colour (#4CC2FF dark theme)
#   3) both renders must agree on the bar geometry (same track/thumb rect from `paint-logs:`),
#      so the only difference sampled is the colour
#
# Each render runs its own COPY of the exe, so each has its own native-diag.log (the exe truncates
# its log at startup -- reading one shared log would compare render #2 against render #1's numbers).
#
# No window is created, no cursor moves, the user's desktop is untouched.
# ASCII only (PS 5.1 reads BOM-less scripts as GBK).

param(
    [string]$Exe = "D:\code\DeepSeekHarness\learn-helper\native\target\release\learn-helper-native.exe",
    [string]$OutDir = $env:TEMP
)

$ErrorActionPreference = 'Stop'
$script:pass = 0
$script:fail = 0

function Check([string]$name, [bool]$ok, [string]$detail = '') {
    if ($ok) { $script:pass++; Write-Host ("  [PASS] " + $name + $(if ($detail) { " -- " + $detail } else { '' })) }
    else { $script:fail++; Write-Host ("  [FAIL] " + $name + $(if ($detail) { " -- " + $detail } else { '' })) }
}

# 24bpp BMP reader: header 54 bytes, rows bottom-up, 3 bytes per pixel, row padded to 4.
function Read-Pixel([string]$Path, [int]$X, [int]$Y) {
    $b = [IO.File]::ReadAllBytes($Path)
    $off = [BitConverter]::ToInt32($b, 10)
    $w = [BitConverter]::ToInt32($b, 18)
    $h = [BitConverter]::ToInt32($b, 22)
    $stride = [int]([math]::Floor(($w * 3 + 3) / 4) * 4)
    $i = $off + ($h - 1 - $Y) * $stride + $X * 3
    return ("#{0:X2}{1:X2}{2:X2}" -f $b[$i + 2], $b[$i + 1], $b[$i])
}

# Render once with its own exe copy + own temp base dir; returns @{ bmp; diag }.
function Render([string]$hover, [string]$tag) {
    $dir = Join-Path $env:TEMP ("lh-hovercheck-" + $tag + "-" + (Get-Random))
    New-Item -ItemType Directory -Path $dir -Force | Out-Null
    $exe2 = Join-Path $dir 'learn-helper-native.exe'
    Copy-Item $Exe $exe2 -Force
    $bmp = Join-Path $OutDir ("logbar-hover-" + $tag + ".bmp")
    Remove-Item $bmp -Force -ErrorAction SilentlyContinue
    $env:LH_PROBE_HOVER_BAR = $hover
    $env:LH_PROBE_LOGS = (@(1..40 | ForEach-Object { "hover check line " + $_ }) -join '|')
    $env:LH_BASE_DIR = $dir
    # Guard: this probe does not test the auto-launch-browser feature, and the app arms that
    # feature 4 seconds after start. Several probes run longer than that, so without this the
    # app could really open the user's sandbox browser in the middle of a test (found by audit).
    $env:LH_NO_AUTO_BROWSER = '1'
    $p = Start-Process -FilePath $exe2 -WorkingDirectory $dir -ArgumentList '--render-probe', '1623', '960', $bmp -PassThru -Wait -NoNewWindow
    Remove-Item Env:LH_PROBE_HOVER_BAR -ErrorAction SilentlyContinue
    Remove-Item Env:LH_PROBE_LOGS -ErrorAction SilentlyContinue
    Remove-Item Env:LH_BASE_DIR -ErrorAction SilentlyContinue
    Remove-Item Env:LH_NO_AUTO_BROWSER -ErrorAction SilentlyContinue
    $bmpCreated = Test-Path $bmp
    # The BMP name is fixed per tag and OutDir defaults to %TEMP%, so a render that silently
    # died (e.g. the single-instance mutex made it exit immediately) used to leave a PREVIOUS
    # run's image behind -- and the probe would happily grade that stale file and print PASS.
    # Fail loudly instead (found by audit).
    if (-not $bmpCreated) {
        Write-Host ("  [FATAL] render produced no BMP (" + $tag + "): exit=" + $p.ExitCode + " bmp=" + $bmp)
        Write-Host "          the exe did not render; refusing to grade a stale image."
        exit 1
    }
    return @{ Bmp = $bmp; Diag = (Join-Path $dir 'native-diag.log'); Exit = $p.ExitCode }
}

function Geometry([string]$diagPath) {
    if (-not (Test-Path $diagPath)) { return $null }
    $line = @(Get-Content $diagPath -Encoding UTF8 | Where-Object { $_ -match 'paint-logs:' })[-1]
    if (-not $line) { return $null }
    $g = @{ line = $line }
    if ($line -match 'bar=(\d)') { $g.bar = [int]$Matches[1] }
    if ($line -match 'track_left=(\d+)') { $g.trackLeft = [int]$Matches[1] }
    if ($line -match 'thumb_top=(\d+)') { $g.thumbTop = [int]$Matches[1] }
    if ($line -match 'thumb_h=(\d+)') { $g.thumbH = [int]$Matches[1] }
    if ($line -match 'hover=(\d)') { $g.hover = [int]$Matches[1] }
    return $g
}

Write-Host "=== log scroll bar hover highlight check (mouse-free) ==="
Write-Host ("exe = " + $Exe)
if (-not (Test-Path $Exe)) { Write-Host "EXE not found"; exit 2 }
$running = @(Get-Process -Name 'learn-helper-native' -ErrorAction SilentlyContinue)
if ($running.Count -gt 0) { Write-Host "ABORT: exe already running (single-instance mutex)"; exit 3 }

Write-Host ""
Write-Host "render 1/2: pointer NOT on the bar"
$off = Render '0' 'off'
$gOff = Geometry $off.Diag
Check "render produced a bar (bar=1)" ($null -ne $gOff -and $gOff.bar -eq 1) ("line=" + $(if ($gOff) { $gOff.line } else { 'none' }))
if ($null -ne $gOff) { Check "pointer reported as off the bar (hover=0)" ($gOff.hover -eq 0) ("hover=" + $gOff.hover) }

Write-Host ""
Write-Host "render 2/2: pointer ON the bar"
$on = Render '1' 'on'
$gOn = Geometry $on.Diag
Check "render produced a bar (bar=1)" ($null -ne $gOn -and $gOn.bar -eq 1) ("line=" + $(if ($gOn) { $gOn.line } else { 'none' }))
if ($null -ne $gOn) { Check "pointer reported as on the bar (hover=1)" ($gOn.hover -eq 1) ("hover=" + $gOn.hover) }

if ($null -eq $gOff -or $null -eq $gOn) {
    Write-Host ""
    Write-Host ("RESULT: " + $script:pass + " passed, " + $script:fail + " failed")
    exit 1
}

Check "both renders agree on the bar geometry" (($gOff.trackLeft -eq $gOn.trackLeft) -and ($gOff.thumbTop -eq $gOn.thumbTop) -and ($gOff.thumbH -eq $gOn.thumbH)) ("off track_left=" + $gOff.trackLeft + " thumb_top=" + $gOff.thumbTop + " thumb_h=" + $gOff.thumbH + " | on track_left=" + $gOn.trackLeft + " thumb_top=" + $gOn.thumbTop + " thumb_h=" + $gOn.thumbH)

# Sample the middle of the thumb in both images.
$sx = $gOn.trackLeft + 7
$sy = $gOn.thumbTop + [int]($gOn.thumbH / 2)
$cOff = Read-Pixel $off.Bmp $sx $sy
$cOn = Read-Pixel $on.Bmp $sx $sy
Write-Host ("  thumb centre (" + $sx + "," + $sy + "): idle=" + $cOff + " hover=" + $cOn)

# Dark theme tokens (ui.rs Colors::for_theme): text_muted #8B96A5, accent #4CC2FF.
Check "idle thumb uses the muted colour" ($cOff -eq '#8B96A5') ("got " + $cOff)
Check "hovered thumb uses the accent colour" ($cOn -eq '#4CC2FF') ("got " + $cOn)
Check "hover really changes the painted pixels" ($cOff -ne $cOn) ($cOff + " -> " + $cOn)

# And the track ABOVE the thumb must stay the plain panel colour in both.
$ty = $gOn.thumbTop - 40
if ($ty -gt 0) {
    $tOff = Read-Pixel $off.Bmp $sx $ty
    $tOn = Read-Pixel $on.Bmp $sx $ty
    Check "track above the thumb is unchanged by hover" ($tOff -eq $tOn) ($tOff + " vs " + $tOn)
}

Write-Host ""
Write-Host ("RESULT: " + $script:pass + " passed, " + $script:fail + " failed")
Write-Host ("images: " + $off.Bmp + " ; " + $on.Bmp)
if ($script:fail -gt 0) { exit 1 }
exit 0
