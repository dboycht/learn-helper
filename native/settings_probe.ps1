# settings_probe.ps1 -- headless verification of the new "Answer Settings" dialog.
#
# What it proves (no human clicking anything):
#   1) the dialog really opens and fills itself from the backend
#      (`settings-hook: opened loaded=True ...`)
#   2) the real click path works: edit backend URL -> click "workers +" twice
#      -> click "Save" -> config.json on disk really changes
#   3) "Save" sends ONLY the changed keys: the pre-seeded llm.api_key / model /
#      retry stay untouched (the backend never echoes api_key back, so a naive
#      implementation would wipe it)
#   4) "Cancel"/Esc writes NOTHING
#
# Isolation: everything runs with LH_BASE_DIR pointed at a temp folder, so the
# user's real learn-helper\config.json is never touched.
#
# NOTE ON LOGS: native-diag.log lives NEXT TO THE EXE (src/trace.rs), not in the
# child working directory. We therefore diff the exe-side log around each case.
#
# Requires: no other instance of the app running (single-instance mutex).
# ASCII only (PS 5.1 reads BOM-less scripts as GBK).

param(
    [string]$Exe = "D:\code\DeepSeekHarness\learn-helper\native\target\release\learn-helper-native.exe",
    [int]$KillAfterSeconds = 40
)

$ErrorActionPreference = 'Stop'
$script:pass = 0
$script:fail = 0
$script:diag = Join-Path (Split-Path -Parent $Exe) 'native-diag.log'

function Check([string]$name, [bool]$ok, [string]$detail = '') {
    if ($ok) {
        $script:pass++
        Write-Host ("  [PASS] " + $name + $(if ($detail) { " -- $detail" } else { '' }))
    } else {
        $script:fail++
        Write-Host ("  [FAIL] " + $name + $(if ($detail) { " -- " + $detail } else { '' }))
    }
}

function New-TempDir([string]$tag) {
    $d = Join-Path $env:TEMP ("lh-settings-probe-" + $tag + "-" + (Get-Random))
    New-Item -ItemType Directory -Path $d -Force | Out-Null
    return $d
}

function Write-Seed([string]$dir, [string]$serverUrl) {
    $seed = [ordered]@{
        server_url = $serverUrl
        answer     = [ordered]@{ mode = 'server'; workers = 4; solver_timeout = 240; retry = 3 }
        llm        = [ordered]@{ base_url = 'https://api.openai.com/v1'; api_key = 'sk-KEEPME-12345'; model = 'gpt-4o-mini' }
        run        = [ordered]@{ video_speed = 2.0; auto_submit = $true }
        note       = 'seeded-by-settings-probe'
    }
    $json = $seed | ConvertTo-Json -Depth 5
    [IO.File]::WriteAllText((Join-Path $dir 'config.json'), $json, (New-Object System.Text.UTF8Encoding($false)))
}

function Read-Cfg([string]$dir) {
    $p = Join-Path $dir 'config.json'
    if (-not (Test-Path $p)) { return $null }
    return (Get-Content $p -Raw -Encoding UTF8 | ConvertFrom-Json)
}

function Diag-Snapshot([string]$dest) {
    if (Test-Path $script:diag) {
        Copy-Item $script:diag $dest -Force
    } else {
        Set-Content -Path $dest -Value '' -Encoding ASCII
    }
}

# Runs one UI case and returns the diag log lines produced by THAT case.
# (The exe truncates native-diag.log on every start, so a per-case copy is the
# only reliable way to attribute lines to a run.)
function Invoke-Ui([string]$dir, [string]$action, [int]$waitSeconds) {
    $snap = Join-Path $env:TEMP ("lh-diag-" + [IO.Path]::GetFileNameWithoutExtension($dir) + "-" + (Get-Random) + ".log")
    $env:LH_BASE_DIR = $dir
    $env:LH_UI_ACTION = $action
    $p = Start-Process -FilePath $Exe -WorkingDirectory $dir -PassThru
    $deadline = (Get-Date).AddSeconds($waitSeconds)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 400
        if ($p.HasExited) { break }
    }
    if (-not $p.HasExited) {
        Write-Host ("  (case still running after " + $waitSeconds + "s -> force-kill ui pid " + $p.Id + ")")
        # copy the log BEFORE killing: the trace flushes every line, and we want this run's lines
        Diag-Snapshot $snap
        Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
        Start-Sleep -Milliseconds 1500
    } else {
        Diag-Snapshot $snap
    }
    Remove-Item Env:LH_BASE_DIR -ErrorAction SilentlyContinue
    Remove-Item Env:LH_UI_ACTION -ErrorAction SilentlyContinue
    if (-not (Test-Path $snap)) { return @() }
    return @(Get-Content $snap -Encoding UTF8 -ErrorAction SilentlyContinue)
}

Write-Host "=== settings dialog probe ==="
Write-Host ("exe  = " + $Exe)
Write-Host ("diag = " + $script:diag)
if (-not (Test-Path $Exe)) { Write-Host "EXE not found"; exit 2 }

$running = @(Get-Process -Name 'learn-helper-native' -ErrorAction SilentlyContinue)
if ($running.Count -gt 0) {
    Write-Host "ABORT: learn-helper-native already running (single-instance mutex) -> results would be bogus"
    exit 3
}

# ---------------------------------------------------------------- CASE 1: save
Write-Host ""
Write-Host "CASE 1: edit backend url + workers++ -> Save"
$d1 = New-TempDir 'save'
Write-Seed $d1 'http://127.0.0.1:8000'
$log1 = Invoke-Ui $d1 'settings_save' $KillAfterSeconds
$cfg1 = Read-Cfg $d1

Check "dialog created" (@($log1 | Where-Object { $_ -match 'settings: window shown' }).Count -gt 0)
Check "dialog opened + loaded settings from backend" (@($log1 | Where-Object { $_ -match 'settings-hook: opened loaded=True' }).Count -gt 0)
$saved = @($log1 | Where-Object { $_ -match 'settings: save ok' }).Count -gt 0
if ($saved) {
    Check "save round-trip ok (PUT /api/settings)" $true
} else {
    $why = @($log1 | Where-Object { $_ -match 'save failed|save still open|CreateWindowExW' })
    Check "save round-trip ok (PUT /api/settings)" $false ($why -join ' | ')
}

if ($null -ne $cfg1) {
    Check "server_url written (field replaced, not appended)" ($cfg1.server_url -eq 'http://127.0.0.1:18080') ("got '" + $cfg1.server_url + "'")
    Check "answer.workers 4 -> 6 (two clicks on +)" ($cfg1.answer.workers -eq 6) ("got " + $cfg1.answer.workers)
    Check "answer.mode untouched" ($cfg1.answer.mode -eq 'server') ("got '" + $cfg1.answer.mode + "'")
    Check "answer.retry untouched (3)" ($cfg1.answer.retry -eq 3) ("got " + $cfg1.answer.retry)
    Check "llm.api_key preserved (blank field = do not modify)" ($cfg1.llm.api_key -eq 'sk-KEEPME-12345') ("got '" + $cfg1.llm.api_key + "'")
    Check "llm.model untouched" ($cfg1.llm.model -eq 'gpt-4o-mini') ("got '" + $cfg1.llm.model + "'")
    Check "unrelated key kept (merge, not overwrite)" ($cfg1.note -eq 'seeded-by-settings-probe')
} else {
    Check "config.json readable after save" $false
}

# -------------------------------------------------------------- CASE 2: cancel
Write-Host ""
Write-Host "CASE 2: edit backend url -> Esc (must NOT write)"
$d2 = New-TempDir 'cancel'
Write-Seed $d2 'http://127.0.0.1:8000'
$log2 = Invoke-Ui $d2 'settings_cancel' 30
$cfg2 = Read-Cfg $d2

Check "cancel path replaced the field in memory" (@($log2 | Where-Object { $_ -match 'cancel path url=http://127.0.0.1:19999' }).Count -gt 0)
Check "Esc really closed the dialog" (@($log2 | Where-Object { $_ -match 'after escape window_alive=False' }).Count -gt 0)
Check "cancel wrote no PUT" (@($log2 | Where-Object { $_ -match 'settings: save ok' }).Count -eq 0)
if ($null -ne $cfg2) {
    Check "config.json untouched after cancel" ($cfg2.server_url -eq 'http://127.0.0.1:8000') ("got '" + $cfg2.server_url + "'")
    Check "cancel: api_key still there" ($cfg2.llm.api_key -eq 'sk-KEEPME-12345')
} else {
    Check "config.json still present after cancel" $false
}

# -------------------------------------------------------------- CASE 3: open only
Write-Host ""
Write-Host "CASE 3: plain open (no edit, no save)"
$d3 = New-TempDir 'open'
Write-Seed $d3 'http://127.0.0.1:8000'
$log3 = Invoke-Ui $d3 'settings' 25
$cfg3 = Read-Cfg $d3
Check "plain open loads settings" (@($log3 | Where-Object { $_ -match 'settings-hook: opened loaded=True' }).Count -gt 0)
Check "plain open did not save" (@($log3 | Where-Object { $_ -match 'settings: save' }).Count -eq 0)
if ($null -ne $cfg3) {
    Check "plain open did not touch config" ($cfg3.server_url -eq 'http://127.0.0.1:8000')
}

Write-Host ""
Write-Host ("RESULT: " + $script:pass + " passed, " + $script:fail + " failed")
Write-Host ("temp dirs kept for inspection: " + $d1 + " ; " + $d2 + " ; " + $d3)
if ($script:fail -gt 0) { exit 1 }
exit 0
