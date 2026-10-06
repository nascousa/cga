@echo off
setlocal
set "CGA_COMPOSE=%USERPROFILE%\.nasco\docker\main\cga\compose.json"
if not exist "%CGA_COMPOSE%" (
  echo ERROR: CGA Dev deployment was not found at "%CGA_COMPOSE%".
  exit /b 1
)
docker compose -f "%CGA_COMPOSE%" up -d --no-build --pull never --wait --wait-timeout 180
if errorlevel 1 exit /b 1
echo CGA Admin: http://localhost:18001/admin
