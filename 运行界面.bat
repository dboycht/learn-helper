@echo off
chcp 65001 >nul
rem ============================================================
rem  learn-helper 2.1.4 -- launcher (PySide6 / Qt6 frontend)
rem
rem  The product UI is now built with PySide6 (Qt6): native window
rem  frame, so moving/resizing/snap-layouts are handled by Windows
rem  itself. The old hand-written Rust Win32/GDI UI (native\) is kept
rem  as a fallback -- it is what "trembled" while dragging.
rem
rem  Fallbacks:
rem      native\target\release\learn-helper-native.exe   (Rust, no runtime needed)
rem      winui\RunWinUI.bat                              (WinUI 3)
rem ============================================================

set "PYW=py -3.12"
set "APPDIR=%~dp0"

rem Prefer pythonw so no console window flashes; fall back to py.
where pythonw >nul 2>nul
if %errorlevel%==0 (
    set "PYW=pythonw"
)

if not exist "%~dp0frontend\app.py" (
    echo.
    echo   [X] Missing frontend\app.py
    echo.
    pause
    exit /b 1
)

echo.
echo   Starting learn-helper ^(PySide6 frontend^) ...
echo   Diagnostics log: %~dp0native-diag.log
echo   Backend log:     %~dp0logs\learn_helper.log
echo.

pushd "%~dp0"
if /i "%PYW%"=="pythonw" (
    start "" pythonw -m frontend.app
) else (
    start "" py -3.12 -m frontend.app
)
popd
exit /b 0
