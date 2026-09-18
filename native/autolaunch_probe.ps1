# autolaunch_probe.ps1 -- headless verification of "open the sandbox browser on startup" (2.1.3).
#
# Background: the old Tk build auto-opened the sandbox browser right after the UI came up
# (`auto_launch_browser_on_start`). The native 2.x rewrite only launched it after the user clicked
# "refresh pages", so this behaviour had to be restored -- and this probe is what proves it works.
#
# What it proves (nobody clicks anything):
#   1) with run.auto_launch_browser = true (the default) the UI sends `launch_browser` a few
#      seconds after start, the backend answers, and a browser really listens on CDP port 9222
#   2) the seeded config.json keeps auto_launch_browser = true (nothing silently rewrote it)
#   3) with run.auto_launch_browser = false the UI still sends the command, but the backend
#      answers "skipped" and **no browser is launched** and no new 9222 appears
#
# Safety: it only touches the sandbox profile under its own temp LH_BASE_DIR, and it shuts down
# whatever browser instance it started (CDP Browser.close + process fallback) before exiting.
# It never moves the mouse. ASCII only (PS 5.1 reads BOM-less scripts as GBK).

param(
    [string]$Exe = "D:\code\DeepSeekHarness\learn-helper\native\target\release\learn-helper-native.exe",
    [int]$WaitSeconds = 30,
    [int]$PortWaitSeconds = 20
)

$ErrorActionPreference = 'Stop'
$script:pass = 0
$script:fail = 0
$script:diag = Join-Path (Split-Path -Parent $Exe) 'native-diag.log'

function Check([string]$name, [bool]$ok, [string]$detail = '') {
    if ($ok) { $script:pass++; Write-Host ("  [PASS] " + $name + $(if ($detail) { " -- " + $detail } else { '' })) }
    else { $script:fail++; Write-Host ("  [FAIL] " + $name + $(if ($detail) { " -- " + $detail } else { '' })) }
}

function New-TempDir([string]$tag) {
    $d = Join-Path $env:TEMP ("lh-autolaunch-" + $tag + "-" + (Get-Random))
    New-Item -ItemType Directory -Path $d -Force | Out-Null
    return $d
}

function Reset-Diag() { Remove-Item $script:diag -Force -ErrorAction SilentlyContinue }

function Diag-Lines([string]$snap) {
    if (Test-Path $script:diag) { Copy-Item $script:diag $snap -Force } else { Set-Content -Path $snap -Value '' -Encoding ASCII }
    return @(Get-Content $snap -Encoding UTF8 -ErrorAction SilentlyContinue)
}

function Wait-Diag([string]$snap, [string]$pattern, [int]$timeoutMs = 12000) {
    $deadline = (Get-Date).AddMilliseconds($timeoutMs)
    while ((Get-Date) -lt $deadline) {
        $lines = Diag-Lines $snap
        if (@($lines | Where-Object { $_ -match $pattern }).Count -gt 0) { return $lines }
        Start-Sleep -Milliseconds 200
    }
    return (Diag-Lines $snap)
}

function Wait-NoInstance([int]$timeoutSeconds = 15) {
    $deadline = (Get-Date).AddSeconds($timeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (@(Get-Process -Name 'learn-helper-native' -ErrorAction SilentlyContinue).Count -eq 0) { return $true }
        Start-Sleep -Milliseconds 300
    }
    return $false
}

function Port-Open() {
    try {
        $c = New-Object System.Net.Sockets.TcpClient
        $iar = $c.BeginConnect('127.0.0.1', 9222, $null, $null)
        $ok = $iar.AsyncWaitHandle.WaitOne(400, $false)
        if ($ok) { $c.EndConnect($iar) }
        $c.Close()
        return $ok
    } catch { return $false }
}

# Which browser processes exist right now? (We only ever kill instances we started.)
function Browser-Pids() {
    $names = @('msedge', 'chrome')
    $out = @()
    foreach ($n in $names) {
        $out += @(Get-Process -Name $n -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id)
    }
    return $out
}

# Ask the sandbox browser to close over CDP (graceful), then fall back to killing the new pids.
function Close-Sandbox([int[]]$known) {
    try {
        $body = '{"id":1,"method":"Browser.close"}'
        $r = Invoke-WebRequest -Uri 'http://127.0.0.1:9222/json/version' -UseBasicParsing -TimeoutSec 3
        $ver = $r.Content | ConvertFrom-Json
        $ws = $ver.webSocketDebuggerUrl
        if ($ws) {
            Add-Type -AssemblyName System.Net.WebSockets.Client -ErrorAction SilentlyContinue
            $ws = $ws -replace 'ws://127\.0\.0\.1:9222', 'ws://127.0.0.1:9222'
            $cli = New-Object System.Net.WebSockets.ClientWebSocket
            $cli.ConnectAsync([Uri]$ws, [Threading.CancellationToken]::None).Wait(3000) | Out-Null
            $bytes = [Text.Encoding]::UTF8.GetBytes($body)
            $seg = New-Object System.ArraySegment[byte] -ArgumentList @(,$bytes)
            $cli.SendAsync($seg, [Net.WebSockets.WebSocketMessageType]::Text, $true, [Threading.CancellationToken]::None).Wait(3000) | Out-Null
            $cli.Dispose()
        }
    } catch { }
    Start-Sleep -Milliseconds 1500
    foreach ($p in (Browser-Pids)) {
        if ($known -notcontains $p) {
            Stop-Process -Id $p -Force -ErrorAction SilentlyContinue
        }
    }
    Start-Sleep -Milliseconds 800
}

function Seed-Config([string]$dir, [bool]$auto) {
    $cfg = @{
        server_url = 'http://127.0.0.1:9'
        answer     = @{ mode = 'server' }
        run        = @{ auto_launch_browser = $auto; auto_submit = $true; video_speed = 2.0 }
    }
    $json = $cfg | ConvertTo-Json -Depth 5
    [IO.File]::WriteAllText((Join-Path $dir 'config.json'), $json, (New-Object System.Text.UTF8Encoding($false)))
}

Write-Host "=== sandbox browser auto-launch probe ==="
Write-Host ("exe  = " + $Exe)
Write-Host ("diag = " + $script:diag)
if (-not (Test-Path $Exe)) { Write-Host "EXE not found"; exit 2 }
$running = @(Get-Process -Name 'learn-helper-native' -ErrorAction SilentlyContinue)
if ($running.Count -gt 0) { Write-Host "ABORT: learn-helper-native already running (single-instance mutex)"; exit 3 }

$portWasOpen = Port-Open
Write-Host ("CDP 9222 already open before the probe = " + $portWasOpen)
$browsersBefore = @(Browser-Pids)

# ------------------------------------------------------------------ CASE 1: default = launch
Write-Host ""
Write-Host "CASE 1: run.auto_launch_browser = true -> the sandbox browser must open"
$d1 = New-TempDir 'on'
Seed-Config $d1 $true
if (-not (Wait-NoInstance)) { Write-Host "  WARNING: previous instance still running" }
$env:LH_BASE_DIR = $d1
Remove-Item Env:LH_UI_ACTION -ErrorAction SilentlyContinue
Reset-Diag
$snap1 = Join-Path $env:TEMP ("lh-autolaunch-1-" + (Get-Random) + ".log")
$p1 = Start-Process -FilePath $Exe -WorkingDirectory $d1 -PassThru

$log1 = Wait-Diag $snap1 'launch_browser' 20000
Check "UI asked the backend to open the browser (launch_browser)" (@($log1 | Where-Object { $_ -match 'launch_browser' }).Count -gt 0)
$answer = @($log1 | Where-Object { $_ -match 'launch_browser' })[-1]

# Wait for the browser to actually listen on 9222 (cold start can take a while).
$opened = $false
$deadline = (Get-Date).AddSeconds($PortWaitSeconds)
while ((Get-Date) -lt $deadline) {
    if (Port-Open) { $opened = $true; break }
    Start-Sleep -Milliseconds 500
}
Check "CDP port 9222 became reachable" $opened
$log1b = Diag-Lines $snap1
$log1b2 = @($log1b)
Check "backend reported the browser action in the log" (@($log1b2 | Where-Object { $_ -match 'launch_browser' }).Count -gt 0) ("line=" + $answer)

$cfg1 = Get-Content (Join-Path $d1 'config.json') -Raw -Encoding UTF8 | ConvertFrom-Json
Check "config.json still says auto_launch_browser = true" ($cfg1.run.auto_launch_browser -eq $true)

# Shut the sandbox browser down (this probe started it) before the next case.
Close-Sandbox $browsersBefore
if (-not $p1.HasExited) { Stop-Process -Id $p1.Id -Force -ErrorAction SilentlyContinue }
Start-Sleep -Milliseconds 1200
Remove-Item Env:LH_BASE_DIR -ErrorAction SilentlyContinue

# ------------------------------------------------------------------ CASE 2: disabled = skip
Write-Host ""
Write-Host "CASE 2: run.auto_launch_browser = false -> the UI asks, the backend skips, nothing opens"
$d2 = New-TempDir 'off'
Seed-Config $d2 $false
if (-not (Wait-NoInstance)) { Write-Host "  WARNING: previous instance still running" }
$env:LH_BASE_DIR = $d2
Reset-Diag
$snap2 = Join-Path $env:TEMP ("lh-autolaunch-2-" + (Get-Random) + ".log")
$p2 = Start-Process -FilePath $Exe -WorkingDirectory $d2 -PassThru

$log2 = Wait-Diag $snap2 'launch_browser' 20000
Check "UI still asked the backend (the decision lives in the backend)" (@($log2 | Where-Object { $_ -match 'launch_browser' }).Count -gt 0)
Start-Sleep -Seconds 6
$log2b = Diag-Lines $snap2
# Two independent pieces of evidence that the backend REFUSED (not just "nothing appeared"):
#   a) the app records the backend's answer in its diagnostics (`ui: launch_browser -> ...`),
#   b) the backend writes the same conclusion into its own file log (tagged with its own
#      browser marker).
# The interface's log panel only lives in memory, so without these an external probe could only
# prove "no browser showed up" -- which is also true when the app is simply broken.
$skipLine = @($log2b | Where-Object { $_ -match 'launch_browser -> ' })[-1]
Check "the app recorded the backend's answer" ($null -ne $skipLine) ("line=" + $skipLine)
$backendLog = Join-Path $d2 'logs\learn_helper.log'
$backLines = @(Get-Content $backendLog -Encoding UTF8 -ErrorAction SilentlyContinue)
# The backend writes the decision through a logger line shaped like
#   "2026-09-18 17:39:16 [INFO] [<browser-tag>] <decision text>"
# so the marker is "] [" + the browser tag + "]".
#
# IMPORTANT: the tags are built from code points (\uXXXX), NOT written as literal Chinese.
# This script must stay pure ASCII: PS 5.1 reads BOM-less files as GBK, so a Chinese literal
# here gets decoded into mojibake, the regex silently stops matching, and the check fails while
# the log clearly contains the line (exactly what happened on the first run of this probe -- E55).
$browserTag = [string]([char]0x6D4F + [char]0x89C8 + [char]0x5668)          # "browser"
$skipTag = [string]([char]0x8DF3 + [char]0x8FC7)                            # "skipped"
$decisionMarker = '] [' + $browserTag + ']'
Check "the backend logged the auto-launch decision" (@($backLines | Where-Object { $_ -match [regex]::Escape($decisionMarker) }).Count -gt 0) ("log=" + $backendLog)
Check "the logged decision says it was skipped" (@($backLines | Where-Object { ($_ -match [regex]::Escape($decisionMarker)) -and ($_ -match [regex]::Escape($skipTag)) }).Count -gt 0)
$portNow = Port-Open
Check "no browser was launched (9222 still closed)" ((-not $portNow) -or $portWasOpen) ("port open now = " + $portNow)
$cfg2 = Get-Content (Join-Path $d2 'config.json') -Raw -Encoding UTF8 | ConvertFrom-Json
Check "config.json still says auto_launch_browser = false" ($cfg2.run.auto_launch_browser -eq $false)

if (-not $p2.HasExited) { Stop-Process -Id $p2.Id -Force -ErrorAction SilentlyContinue }
Start-Sleep -Milliseconds 1200
Remove-Item Env:LH_BASE_DIR -ErrorAction SilentlyContinue

# ------------------------------------------------------------------ cleanup
$left = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -like '*learn-helper*' })
Check "backend process cleaned up" ($left.Count -eq 0) ("left=" + $left.Count)

Write-Host ""
Write-Host ("RESULT: " + $script:pass + " passed, " + $script:fail + " failed")
Write-Host ("temp dirs kept for inspection: " + $d1 + " ; " + $d2)
if ($script:fail -gt 0) { exit 1 }
exit 0
