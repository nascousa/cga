@echo off
setlocal
set "CGA_COMPOSE=%USERPROFILE%\.nasco\docker\main\cga\compose.json"
if exist "%CGA_COMPOSE%" goto recovered
cd /d "%~dp0"
powershell -NoLogo -NoProfile -ExecutionPolicy Bypass -File ".\src\scripts\start-desktop.ps1" start -OpenBrowser:$true
exit /b %ERRORLEVEL%

:recovered
docker compose -f "%CGA_COMPOSE%" up -d --no-build --pull never --wait --wait-timeout 180
if errorlevel 1 exit /b 1
echo CGA Admin: http://localhost:18001/admin
