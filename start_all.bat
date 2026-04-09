@echo off
echo ================================================
echo   ICT Trading Bot - Starting Dashboard System
echo ================================================
echo.
cd /d "%~dp0"

REM Check if sidecar is already running
curl -s http://localhost:9000/status >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    echo ERROR: Bot Manager sidecar is already running!
    echo.
    for /f "delims=" %%a in ('curl -s http://localhost:9000/status') do echo   Status: %%a
    echo.
    echo Please run stop_all.bat first to shut down the existing system.
    echo.
    pause
    exit /b 1
)

python start_dashboard.py
