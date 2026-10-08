@echo off
REM Double-click this file to start the Quoting Engine on Windows.

cd /d "%~dp0backend"

echo Alpine Edge Carpentry - Quoting Engine
echo ========================================
echo.

where python >nul 2>nul
if %errorlevel% neq 0 (
    echo Python isn't installed on this PC.
    echo Install it from https://www.python.org/downloads/ and then run this again.
    echo IMPORTANT: during install, tick the box that says "Add Python to PATH".
    pause
    exit /b 1
)

echo Installing what's needed ^(only takes a while the first time^)...
python -m pip install --quiet -r requirements.txt

echo.
echo Starting the server...
echo Your browser will open automatically in a few seconds.
echo To stop the server later, close this window.
echo.

start "" /min cmd /c "timeout /t 3 >nul && start http://127.0.0.1:8000"

python -m uvicorn main:app --host 127.0.0.1 --port 8000

pause
