# verify_panel.ps1 -- assert the appearance panel leaves the style switch usable.
#
# Pure ASCII (rules/01 section 8.2).
#
# Regression for the 2026-09-12 bug: the "glass" radio button was disabled whenever
# the app was not already in glass mode, so once the user picked "plain" the style
# could never be switched back. A disabled control is invisible in a crash-free run,
# so this asserts against the panel-state dump the app writes to ui-diag.log.

$ErrorActionPreference = 'Continue'
$dir = $PSScriptRoot
$out = Join-Path $dir 'bin\x64\Debug\net9.0-windows10.0.19041.0\win-x64'
$exe = Join-Path $out 'LearnHelper.App.exe'
$cfg = Join-Path $out 'ui-settings.json'
$diag = Join-Path $out 'ui-diag.log'

if (-not (Test-Path $exe)) { throw "exe not found, build first: $exe" }

$fails = 0
function Assert($cond, $msg) {
    if ($cond) { Write-Output "  PASS  $msg" } else { Write-Output "  FAIL  $msg"; $script:fails++ }
}

function Run-App {
    param([hashtable]$Env = @{}, [string]$StartMode, [int]$Seconds = 5)
    if (Test-Path $diag) { Remove-Item $diag -Force }
    if ($StartMode) {
        # Seed the settings file so the app starts in the requested style.
        (@{ UiMode = $StartMode; ThemeMode = 'dark'; GlassKind = 'thin'; CardOpacity = 0.72;
            TintOpacity = 0.55; LuminosityOpacity = 0.0; TintColor = '#101418'; FallbackDim = 0.0 } |
            ConvertTo-Json) | Set-Content -Path $cfg -Encoding UTF8
    }
    foreach ($k in $Env.Keys) { Set-Item -Path "Env:$k" -Value $Env[$k] }
    $p = Start-Process -FilePath $exe -PassThru
    Start-Sleep -Seconds $Seconds
    $p.Refresh()
    $alive = -not $p.HasExited
    if ($alive) { $p.Kill(); $p.WaitForExit(4000) | Out-Null }
    foreach ($k in $Env.Keys) { Remove-Item -Path "Env:$k" -ErrorAction SilentlyContinue }
    return $alive
}

function PanelLine($tag) {
    if (-not (Test-Path $diag)) { return $null }
    return (Get-Content -Encoding UTF8 $diag | Where-Object { $_ -match "panel\[$tag\]" } | Select-Object -Last 1)
}

function GetFlag($line, $name) {
    if (-not $line) { return $null }
    if ($line -match "$name=(\w+)") { return $Matches[1] }
    return $null
}

# ---- 1. start in plain: the STYLE switch must still be usable ---------------
Write-Output 'CASE 1: start in plain mode -> glass style must remain selectable'
$alive = Run-App -StartMode 'plain'
Assert $alive 'app stayed alive'
$line = PanelLine 'startup'
Assert ($null -ne $line) 'panel state was dumped at startup'
if ($line) {
    Write-Output "        $line"
    Assert ((GetFlag $line 'styleGlass') -eq 'True') 'glass style radio ENABLED while in plain mode  <-- the reported bug'
    Assert ((GetFlag $line 'stylePlain') -eq 'True') 'plain style radio enabled'
    Assert ((GetFlag $line 'card') -eq 'True') 'card opacity slider enabled'
    Assert ((GetFlag $line 'tint') -eq 'False') 'tint slider disabled in plain mode (glass-only knob)'
    Assert ((GetFlag $line 'thin') -eq 'False') 'glass kind disabled in plain mode'
}

# ---- 2. start in glass: knobs must be live --------------------------------
Write-Output 'CASE 2: start in glass mode -> glass knobs must be live'
$alive = Run-App -StartMode 'glass'
Assert $alive 'app stayed alive'
$line = PanelLine 'startup'
if ($line) {
    Write-Output "        $line"
    Assert ((GetFlag $line 'styleGlass') -eq 'True') 'glass style radio enabled'
    Assert ((GetFlag $line 'stylePlain') -eq 'True') 'plain style radio enabled'
    Assert ((GetFlag $line 'tint') -eq 'True') 'tint slider enabled in glass mode'
    Assert ((GetFlag $line 'lum') -eq 'True') 'luminosity slider enabled in glass mode'
    Assert ((GetFlag $line 'thin') -eq 'True') 'glass kind enabled in glass mode'
}

# ---- 3. the material must actually ATTACH when the OS supports it ---------
Write-Output 'CASE 3: material must attach, not silently fall back'
$alive = Run-App -StartMode 'glass'
$diagText = if (Test-Path $diag) { Get-Content -Raw -Encoding UTF8 $diag } else { '' }
Assert ($diagText -match 'material support:') 'material support line present'
Assert ($diagText -match 'material: SystemBackdrop assigned and bound') 'material reported as attached'
Assert ($diagText -match 'attached=True') 'attached=True recorded  <-- regressed when a ctor callback threw'

# Cross-check: if the OS says acrylic is supported, we must not have fallen back.
if ($diagText -match 'material support: acrylic=(True|False) mica=(True|False) attached=(True|False)') {
    $acrylic = $Matches[1]
    $attached = $Matches[3]
    Assert (($acrylic -eq 'False') -or ($attached -eq 'True')) `
        "acrylic supported => material attached (acrylic=$acrylic attached=$attached)"
}

# Any 'attach FAILED' means our own code broke the attach, not the OS.
Assert ($diagText -notmatch 'attach FAILED') 'no attach failure recorded'

# ---- 4. appearance must be applied live, on every change ------------------
Write-Output 'CASE 4: every colour token must be pushed on each appearance change'
Assert ($diagText -match 'palette\[(light|dark)\]: applied 17 tokens') 'full palette (17 tokens) applied at startup'
Assert ($diagText -notmatch 'were not declared in App.xaml') 'no token missing from App.xaml'

# Light and dark must both be appliable without error.
$alive = Run-App -Env @{ LH_UI_THEME = 'light' } -Seconds 5
Assert $alive 'app stayed alive with the light palette'
$diagText = Get-Content -Raw -Encoding UTF8 $diag
Assert ($diagText -match 'palette\[light\]: applied 17 tokens') 'light palette applied'
Assert ($diagText -notmatch 'TryApply FAILED') 'applying the light palette did not fail'

# The text-box surface must be FULLY OPAQUE: this is where text is read, and a
# translucent light surface over a dark wallpaper is what produced the "the text box
# colours are inverted" report.
Assert ($diagText -match 'Surface=FF(FFFFFF|171C22)') 'text-box surface is opaque (alpha FF)'

# Both themes must end up with dark-on-light / light-on-dark, not swapped.
$lightLine = (Get-Content -Encoding UTF8 $diag | Where-Object { $_ -match 'Base=FFF2F4F7' } | Select-Object -Last 1)
Assert ($null -ne $lightLine) 'light palette dump present'
if ($lightLine) {
    Assert ($lightLine -match 'TextMain=FF15202B') 'light theme uses DARK text on light surfaces'
}
$darkLine = (Get-Content -Encoding UTF8 $diag | Where-Object { $_ -match 'Base=FF101418' } | Select-Object -Last 1)
Assert ($null -ne $darkLine) 'dark palette dump present'
if ($darkLine) {
    Assert ($darkLine -match 'TextMain=FFE8EDF2') 'dark theme uses LIGHT text on dark surfaces'
}

# ---- 5. developer colour overrides must actually reach the palette ---------
Write-Output 'CASE 5: developer overrides must be applied to the live palette'
if (Test-Path $diag) { Remove-Item $diag -Force }
(@{ UiMode = 'glass'; ThemeMode = 'dark'; GlassKind = 'thin'; CardOpacity = 0.85;
    TintOpacity = 0.55; LuminosityOpacity = 0.0; TintColor = '#101418'; FallbackDim = 0.0;
    Overrides = @{ dark = @{ textMain = '#F0F4F8'; accent = '#FF7AD1' }; light = @{} } } |
    ConvertTo-Json -Depth 6) | Set-Content -Path $cfg -Encoding UTF8
$ovr = Start-Process -FilePath $exe -PassThru
Start-Sleep -Seconds 5
$ovr.Refresh()
$ovrAlive = -not $ovr.HasExited
if ($ovrAlive) { $ovr.Kill(); $ovr.WaitForExit(4000) | Out-Null }
Assert $ovrAlive 'app stayed alive with overrides present'
$ovrDiag = Get-Content -Raw -Encoding UTF8 $diag
Assert ($ovrDiag -match 'using 2 override\(s\) for dark') 'both dark overrides were picked up'
Assert ($ovrDiag -match 'TextMain=FFF0F4F8') 'textMain override reached the applied palette  <-- regressed when Build forgot ApplyOverrides'

# ---- 6. both drawers must open without crashing ---------------------------
Write-Output 'CASE 6: appearance and developer drawers must open cleanly'
if (Test-Path $cfg) { Remove-Item $cfg -Force }
$alive = Run-App -Env @{ LH_UI_OPEN = 'appearance' } -Seconds 6
Assert $alive 'app stayed alive with the appearance drawer open'
$d6 = Get-Content -Raw -Encoding UTF8 $diag
Assert ($d6 -match 'appearance drawer: opened') 'appearance drawer opened'
Assert ($d6 -notmatch 'UNHANDLED') 'no unhandled exception with the appearance drawer open'

$alive = Run-App -Env @{ LH_UI_OPEN = 'dev' } -Seconds 6
Assert $alive 'app stayed alive with the developer drawer open'
$d6 = Get-Content -Raw -Encoding UTF8 $diag
Assert ($d6 -match 'dev drawer: opened') 'developer drawer opened'
Assert ($d6 -notmatch 'UNHANDLED') 'no unhandled exception with the developer drawer open'

# ---- 7. unreadable text must be auto-corrected, not shipped ----------------
Write-Output 'CASE 7: bad text colours must be pushed back to readable'
$alive = Run-App -Env @{ LH_UI_THEME = 'light' } -Seconds 1   # seed light theme
if (Test-Path $diag) { Remove-Item $diag -Force }
# Poison: white text on the light surface. Rule: light background -> dark text.
(@{ UiMode = 'glass'; ThemeMode = 'light'; GlassKind = 'thin'; CardOpacity = 0.88;
    TintOpacity = 0.35; LuminosityOpacity = 0.25; TintColor = '#F2F4F7'; FallbackDim = 0.0;
    Overrides = @{ light = @{ textMain = '#FFFFFF'; textSub = '#EEEEEE' }; dark = @{} } } |
    ConvertTo-Json -Depth 6) | Set-Content -Path $cfg -Encoding UTF8
$p = Start-Process -FilePath $exe -PassThru
Start-Sleep -Seconds 5
$p.Refresh()
$alive = -not $p.HasExited
if ($alive) { $p.Kill(); $p.WaitForExit(4000) | Out-Null }
Assert $alive 'app stayed alive with poisoned colours'
$d7 = Get-Content -Raw -Encoding UTF8 $diag
Assert ($d7 -match 'TextMain=FF(7|6|5|4|3|2|1|0)') 'white text on a light surface was darkened (light bg -> dark text)'
Assert ($d7 -notmatch 'TextMain=FFFFFFFF') 'the unreadable white-on-white value is NOT what got applied'

# ---- 8. developer drawer chrome + swatch clicks ---------------------------
Write-Output 'CASE 8: dev drawer must be readable and swatch clicks must apply'
if (Test-Path $cfg) { Remove-Item $cfg -Force }
$alive = Run-App -Env @{ LH_UI_OPEN = 'dev'; LH_UI_DEV_SELFTEST = '1'; LH_UI_THEME = 'dark' } -Seconds 6
Assert $alive 'app stayed alive with the dev drawer self-test (dark)'
$d8 = Get-Content -Raw -Encoding UTF8 $diag
Assert ($d8 -match 'editing dark, chrome bg=#161B22/#F2F6FA') 'dark palette edited on dark drawer with light text'
Assert ($d8 -match 'dev selftest: swatch click -> textMain=') 'swatch click reached the draft (dark)'
Assert ($d8 -match 'override stored=#FF00AA') 'swatch colour was stored as an override'
Assert ($d8 -notmatch 'UNHANDLED') 'no unhandled exception in the dev drawer self-test (dark)'

if (Test-Path $cfg) { Remove-Item $cfg -Force }
$alive = Run-App -Env @{ LH_UI_OPEN = 'dev'; LH_UI_DEV_SELFTEST = '1'; LH_UI_THEME = 'light' } -Seconds 6
Assert $alive 'app stayed alive with the dev drawer self-test (light)'
$d8 = Get-Content -Raw -Encoding UTF8 $diag
Assert ($d8 -match 'editing light, chrome bg=#F4F6F9/#12181F') 'light palette edited on light drawer with dark text'
Assert ($d8 -match 'dev selftest: swatch click -> textMain=#') 'swatch click reached the draft (light)'
Assert ($d8 -notmatch 'UNHANDLED') 'no unhandled exception in the dev drawer self-test (light)'

Write-Output ''
if ($fails -eq 0) { Write-Output 'ALL PANEL STATE CHECKS PASSED' }
else { Write-Output "$fails CHECK(S) FAILED" }
exit $fails
