@echo off
powershell -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0src\scripts\start-relay.ps1"
exit /b %ERRORLEVEL%
