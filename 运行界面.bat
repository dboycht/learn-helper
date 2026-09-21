@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul
rem ============================================================
rem  learn-helper 2.1.4 -- launcher (PySide6 / Qt6 frontend)
rem
rem  The product UI is built with PySide6 (Qt6): it uses a real Qt
rem  window, so moving / resizing / snap-layouts are handled by
rem  Windows itself. The old hand-written Rust Win32/GDI UI (native\)
rem  is kept as a fallback -- it is what "trembled" while dragging.
rem
rem  Fallbacks:
rem      native\target\release\learn-helper-native.exe   (Rust, no runtime needed)
rem      winui\RunWinUI.bat                              (WinUI 3)
rem ============================================================

set "APPDIR=%~dp0"

if not exist "%~dp0frontend\main.py" (
    echo.
    echo   [X] Missing frontend\main.py
    echo.
    pause
    exit /b 1
)

rem ---- pick an interpreter that actually HAS PySide6 -------------------------
rem NOTE: do not just use "pythonw" -- it may be a different Python without
rem PySide6, and then the app would exit silently (no console, no error).
set "PYW="
for %%C in ("pythonw" "py -3.12") do (
    if not defined PYW (
        %%C -c "import PySide6" >nul 2>nul
        if !errorlevel!==0 set "PYW=%%~C"
    )
)

if not defined PYW (
    echo.
    echo   [X] No Python with PySide6 found.
    echo       Install it with:
    echo           py -3.12 -m pip install PySide6
    echo.
    echo   You can also run the fallback UI instead:
    echo       native\target\release\learn-helper-native.exe
    echo.
    pause
    exit /b 1
)

echo.
echo   Starting learn-helper ^(PySide6 frontend^) with: %PYW%
echo   Diagnostics log: %~dp0native-diag.log
echo   Backend log:     %~dp0logs\learn_helper.log
echo.

pushd "%~dp0"
start "" %PYW% -m frontend.main
popd
exit /b 0
