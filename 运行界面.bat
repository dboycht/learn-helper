@echo off
chcp 65001 >nul
rem ============================================================
rem  learn-helper 2.x -- native default launcher (developer build)
rem
rem  The product UI since 2.1.1 is the ZERO-DEPENDENCY Rust Win32 app
rem  (native\), a single exe of about 0.3 MB that needs no runtime install.
rem  The former WinUI 3 build is kept as a fallback and has its own
rem  launcher: winui\RunWinUI.bat
rem
rem  If it says "not built yet", build it with:
rem      cd native  &&  cargo build --release
rem ============================================================

set "EXE=%~dp0native\target\release\learn-helper-native.exe"

if not exist "%EXE%" (
    echo.
    echo   [X] Not built yet:
    echo       %EXE%
    echo.
    echo   Build it first with:
    echo       cd native ^&^& cargo build --release
    echo.
    echo   Or try the fallback WinUI build:
    echo       winui\RunWinUI.bat
    echo.
    pause
    exit /b 1
)

echo.
echo   Starting learn-helper ^(native Win32^) ...
echo   Diagnostics log:
echo       %~dp0native\target\release\native-diag.log
echo   Backend log:
echo       %~dp0logs\learn_helper.log
echo.

start "" "%EXE%"
exit /b 0
