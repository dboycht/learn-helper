@echo off
chcp 65001 >nul
rem ============================================================
rem  learn-helper WinUI 3 -- double-click launcher (developer build)
rem
rem  Why this exists: the real exe lives deep inside
rem  winui\bin\x64\Debug\net9.0-windows10.0.19041.0\win-x64\
rem  which is painful to find. This just runs it.
rem
rem  If it says "not built yet", run:
rem      dotnet build winui\LearnHelper.App.csproj -p:Platform=x64
rem ============================================================

set "EXE=%~dp0winui\bin\x64\Debug\net9.0-windows10.0.19041.0\win-x64\LearnHelper.App.exe"

if not exist "%EXE%" (
    echo.
    echo   [X] Not built yet:
    echo       %EXE%
    echo.
    echo   Build it first with:
    echo       dotnet build winui\LearnHelper.App.csproj -p:Platform=x64
    echo.
    pause
    exit /b 1
)

echo.
echo   Starting learn-helper ^(WinUI 3^) ...
echo   Settings file:
echo       %~dp0winui\bin\x64\Debug\net9.0-windows10.0.19041.0\win-x64\ui-settings.json
echo   Diagnostics log:
echo       %~dp0winui\bin\x64\Debug\net9.0-windows10.0.19041.0\win-x64\ui-diag.log
echo.

start "" "%EXE%"
exit /b 0
