@echo off
setlocal
cd /d "%~dp0"
powershell -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; & '.\src\scripts\start-desktop.ps1' start -OpenBrowser $true; exit $LASTEXITCODE"
exit /b %ERRORLEVEL%
