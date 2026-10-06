@echo off
setlocal
cd /d "%~dp0"
powershell -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; & '.\src\scripts\start-desktop.ps1' logs -Detached $false; exit $LASTEXITCODE"
exit /b %ERRORLEVEL%