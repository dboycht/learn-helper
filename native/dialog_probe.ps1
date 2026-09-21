# dialog_probe.ps1 -- verify the platform's "current chapter still has unfinished tasks" modal:
#   1. the modal is detected at all;
#   2. the button we pick is the modal's "skip" button, NOT the "go study" one, and NOT the
#      page's own same-labelled button (a naive "first visible match" search picks the wrong one);
#   3. a page without the modal is not misdetected.
#
# Why this is a standalone probe instead of a case in backend\verify_backend.py: the self-test
# runs right after cases that tear the sandbox browser down (`eng.diagnose()` closes the browser),
# so connecting to CDP from inside it is racy -- and an unreliable test is worse than none
# (project rule). This probe owns its browser lifecycle from start to finish.
#
# The logic under test is Python + DOM, so it is driven through the real backend module
# (`learn_helper.dialog_probe`), which mirrors how the app connects (CDP 9222). A PowerShell
# re-implementation would be a second copy of the logic and would drift -- we deliberately shell out.
#
# ASCII only (rules/01 section 8.2): the Chinese labels live inside the Python/HTML fixtures.

param(
    [string]$Python = 'py',
    [string]$PythonArgs = '-3.10'
)

$ErrorActionPreference = 'Continue'
$repo = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $repo 'backend'

# Run FROM the backend dir so `learn_helper` is importable as a package.
Push-Location $backend
try {
    $argv = @()
    if ($PythonArgs -ne '') { $argv += $PythonArgs }
    $argv += @('-m', 'learn_helper.dialog_probe')
    $out = & $Python @argv 2>&1
    $code = $LASTEXITCODE
} finally {
    Pop-Location
}

$out | ForEach-Object { Write-Output $_ }
if ($code -eq 0) { Write-Output 'RESULT: dialog probe PASSED' }
else { Write-Output 'RESULT: dialog probe FAILED' }
exit $code
