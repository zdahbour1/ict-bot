@echo off
echo ================================================
echo   ICT Trading Bot - Stopping All Services
echo ================================================
echo.
cd /d "%~dp0"

echo [1/3] Stopping bot via sidecar...
curl -s -X POST http://localhost:9000/stop 2>nul
echo.

echo [2/3] Stopping sidecar and bot processes...
taskkill /F /FI "WINDOWTITLE eq bot_manager*" 2>nul
taskkill /F /FI "IMAGENAME eq python.exe" /FI "WINDOWTITLE eq *main.py*" 2>nul
timeout /t 2 /nobreak >nul

echo [3/3] Stopping Docker services...
docker compose down
echo.
echo All services stopped.
pause
