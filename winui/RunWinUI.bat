@echo off
chcp 65001 >nul
rem ============================================================
rem  learn-helper -- WinUI 3 fallback launcher (kept as the back-up line)
rem
rem  Since 2.1.1 the default UI is the native Rust Win32 app
rem  (repo-root 运行界面.bat). This WinUI build is deliberately NOT deleted:
rem  per the project's migration discipline, a rejected technology line is
rem  parked rather than removed until the new one is proven.
rem
rem  Build it with:
rem      dotnet build winui\LearnHelper.App.csproj -p:Platform=x64
rem ============================================================

set "EXE=%~dp0bin\x64\Debug\net9.0-windows10.0.19041.0\win-x64\LearnHelper.App.exe"

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
echo   Starting learn-helper ^(WinUI 3 fallback^) ...
echo   Diagnostics log:
echo       %~dp0bin\x64\Debug\net9.0-windows10.0.19041.0\win-x64\ui-diag.log
echo.

start "" "%EXE%"
exit /b 0
