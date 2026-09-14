# verify_theme.ps1 -- end-to-end check of the appearance settings feature.
#
# Pure ASCII on purpose (rules/01 section 8.2).
# Why this exists: the WinUI window cannot be screenshotted by the agent
# (see ERROR.md E10 / memory/03 section 7), so behaviour is verified through the
# settings file plus process liveness - NOT through synthetic clicks
# (memory/04 section 1 forbids input injection).
#
# It never steals the foreground and never kills unrelated processes.

$ErrorActionPreference = 'Continue'
$dir = $PSScriptRoot
$exe = Join-Path $dir 'bin\x64\Debug\net9.0-windows10.0.19041.0\win-x64\LearnHelper.App.exe'
$cfg = Join-Path $dir 'bin\x64\Debug\net9.0-windows10.0.19041.0\win-x64\ui-settings.json'

if (-not (Test-Path $exe)) { throw "exe not found, build first: $exe" }

$fails = 0
function Assert($cond, $msg) {
    if ($cond) { Write-Output "  PASS  $msg" } else { Write-Output "  FAIL  $msg"; $script:fails++ }
}

function Run-App {
    param([hashtable]$Env = @{}, [int]$Seconds = 5)
    foreach ($k in $Env.Keys) { Set-Item -Path "Env:$k" -Value $Env[$k] }
    $p = Start-Process -FilePath $exe -PassThru
    Start-Sleep -Seconds $Seconds
    $p.Refresh()
    $alive = -not $p.HasExited
    if ($alive) { $p.Kill(); $p.WaitForExit(4000) | Out-Null }
    foreach ($k in $Env.Keys) { Remove-Item -Path "Env:$k" -ErrorAction SilentlyContinue }
    return $alive
}

function Write-Cfg($obj) {
    ($obj | ConvertTo-Json) | Set-Content -Path $cfg -Encoding UTF8
}

function Read-Cfg { return (Get-Content -Raw -Encoding UTF8 $cfg | ConvertFrom-Json) }

# ---- 1. no settings file: defaults materialise, no crash -------------------
Write-Output 'CASE 1: no settings file -> defaults are written by the app'
if (Test-Path $cfg) { Remove-Item $cfg -Force }
$alive = Run-App -Seconds 5
Assert $alive 'app stayed alive for 5s'
Assert (Test-Path $cfg) 'settings file was created on a fresh start'
if (Test-Path $cfg) {
    $j = Read-Cfg
    Assert ($j.UiMode -eq 'glass') 'default UiMode is glass'
    Assert ($j.ThemeMode -eq 'dark') 'default ThemeMode is dark'
    Assert ($j.GlassKind -eq 'thin') 'default GlassKind is thin'
    Assert ([math]::Abs($j.CardOpacity - 0.85) -lt 0.001) 'default CardOpacity is 0.85 (above the readability floor)'
    Assert ([math]::Abs($j.TintOpacity - 0.55) -lt 0.001) 'default TintOpacity is 0.55'
    Assert ([math]::Abs($j.LuminosityOpacity - 0.35) -lt 0.001) 'default LuminosityOpacity is 0.35 (seeded so the material reads as glass)'
}

# ---- 2. existing settings must survive a plain start ----------------------
Write-Output 'CASE 2: a plain start must NOT overwrite an existing choice'
Write-Cfg @{ UiMode = 'plain'; ThemeMode = 'light'; GlassKind = 'base'; CardOpacity = 0.95;
             TintOpacity = 0.33; LuminosityOpacity = 0.2; TintColor = '#F2F4F7'; FallbackDim = 0.0 }$alive = Run-App -Seconds 5
Assert $alive 'app stayed alive for 5s'
$j = Read-Cfg
Assert ($j.UiMode -eq 'plain') 'UiMode plain preserved'
Assert ($j.ThemeMode -eq 'light') 'ThemeMode light preserved'
Assert ($j.GlassKind -eq 'base') 'GlassKind base preserved'
Assert ([math]::Abs($j.CardOpacity - 0.95) -lt 0.001) 'CardOpacity 0.95 preserved'
Assert ([math]::Abs($j.TintOpacity - 0.33) -lt 0.001) 'TintOpacity 0.33 preserved'
Assert ([math]::Abs($j.LuminosityOpacity - 0.2) -lt 0.001) 'LuminosityOpacity 0.2 preserved'

# ---- 3. back to glass + dark ----------------------------------------------
Write-Output 'CASE 3: LH_UI_MODE=glass LH_UI_THEME=dark'
$alive = Run-App -Env @{ LH_UI_MODE = 'glass'; LH_UI_THEME = 'dark' } -Seconds 5
Assert $alive 'app stayed alive for 5s'
$j = Read-Cfg
Assert ($j.UiMode -eq 'glass') 'UiMode persisted as glass'
Assert ($j.ThemeMode -eq 'dark') 'ThemeMode persisted as dark'
Assert ($j.TintColor -eq '#101418') 'TintColor followed the dark base colour'

# ---- 4. glass parameters ---------------------------------------------------
Write-Output 'CASE 4: glass kind + card/tint/luminosity override'
$alive = Run-App -Env @{ LH_UI_GLASS_KIND = 'base'; LH_UI_CARD_OPACITY = '0.88'; LH_UI_TINT_OPACITY = '0.42'; LH_UI_LUMINOSITY = '0.15' } -Seconds 5
Assert $alive 'app stayed alive for 5s'
$j = Read-Cfg
Assert ($j.GlassKind -eq 'base') 'GlassKind persisted as base'
Assert ([math]::Abs($j.CardOpacity - 0.88) -lt 0.001) 'CardOpacity persisted as 0.88 (inside the safe range)'
Assert ([math]::Abs($j.TintOpacity - 0.42) -lt 0.001) 'TintOpacity persisted as 0.42'
Assert ([math]::Abs($j.LuminosityOpacity - 0.15) -lt 0.001) 'LuminosityOpacity persisted as 0.15'

# A card opacity below the readability floor must be raised, not honoured: below ~0.77
# the weakest text tier falls under WCAG 4.5:1 once a wallpaper shows through.
$alive = Run-App -Env @{ LH_UI_CARD_OPACITY = '0.45' } -Seconds 5
Assert $alive 'app stayed alive with an out-of-range card opacity'
$j = Read-Cfg
Assert ([math]::Abs($j.CardOpacity - 0.80) -lt 0.001) 'CardOpacity 0.45 raised to the 0.80 floor (readability)'

# ---- 4b. luminosity default is per theme -----------------------------------
Write-Output 'CASE 4b: each theme must adopt its own luminosity default'
$alive = Run-App -Env @{ LH_UI_THEME = 'dark' } -Seconds 5
$j = Read-Cfg
Assert ([math]::Abs($j.LuminosityOpacity - 0.35) -lt 0.001) 'dark theme defaults LuminosityOpacity to 0.35'
$alive = Run-App -Env @{ LH_UI_THEME = 'light' } -Seconds 5
$j = Read-Cfg
Assert ([math]::Abs($j.LuminosityOpacity - 0.25) -lt 0.001) 'light theme defaults LuminosityOpacity to 0.25'

# ---- 5. out-of-range values get clamped -----------------------------------
Write-Output 'CASE 5: out-of-range values must be clamped, not trusted'
$alive = Run-App -Env @{ LH_UI_TINT_OPACITY = '5.0'; LH_UI_LUMINOSITY = '-3.0'; LH_UI_CARD_OPACITY = '0.0' } -Seconds 5
Assert $alive 'app stayed alive for 5s'
$j = Read-Cfg
Assert ($j.TintOpacity -le 0.95) 'TintOpacity clamped to <= 0.95'
Assert ($j.LuminosityOpacity -ge 0) 'LuminosityOpacity clamped to >= 0'
Assert ($j.CardOpacity -ge 0.80) 'CardOpacity clamped to >= 0.80 (readability floor)'

# ---- 6. invalid enum values are ignored, not fatal -------------------------
Write-Output 'CASE 6: unknown enum values must be ignored gracefully'
$alive = Run-App -Env @{ LH_UI_MODE = 'neon'; LH_UI_THEME = 'neon'; LH_UI_GLASS_KIND = 'vapor' } -Seconds 5
Assert $alive 'app stayed alive for 5s'
$j = Read-Cfg
Assert ($j.UiMode -in @('glass', 'plain')) 'UiMode stayed a valid value'
Assert ($j.ThemeMode -in @('dark', 'light')) 'ThemeMode stayed a valid value'
Assert ($j.GlassKind -in @('thin', 'base')) 'GlassKind stayed a valid value'

# ---- 7. corrupt settings file must not block startup ----------------------
Write-Output 'CASE 7: corrupt settings file must not prevent startup'
Set-Content -Path $cfg -Value '{ this is not json' -Encoding UTF8
$alive = Run-App -Seconds 5
Assert $alive 'app tolerated a corrupt settings file and still started'
if (Test-Path $cfg) {
    $j = Read-Cfg   # would throw if the app left garbage behind
    Assert ($j.ThemeMode -in @('dark', 'light')) 'corrupt file was replaced with valid settings'
}

Write-Output ''
if ($fails -eq 0) { Write-Output 'ALL THEME SETTINGS CHECKS PASSED' }
else { Write-Output "$fails CHECK(S) FAILED" }
exit $fails
