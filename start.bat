@echo off
cd /d "%~dp0"

echo ============================================
echo   CME Automation Tool
echo ============================================
echo.

echo [1/3] Starting Flask server...
start /b python src/app.py

echo [*] Waiting for Flask to be ready...
:wait_flask
timeout /t 2 /nobreak >nul
python -c "import urllib.request; urllib.request.urlopen('http://localhost:5000', timeout=1)" 2>nul
if errorlevel 1 goto wait_flask
echo [OK] Flask is ready

echo.
echo [2/3] Opening browser...
start http://localhost:5000

echo.
echo [3/3] Checking WeChat status...
python -c "from src.wechat.controller import ensure_wechat_ready; import sys; sys.exit(0 if ensure_wechat_ready() else 1)"
if errorlevel 1 (
    echo [*] WeChat not ready, retrying in 10s...
    timeout /t 10 /nobreak >nul
    python -c "from src.wechat.controller import ensure_wechat_ready; import sys; sys.exit(0 if ensure_wechat_ready() else 1)"
    if errorlevel 1 (
        echo.
        echo ============================================
        echo   [FAIL] WeChat is not ready, cannot start
        echo   Please check:
        echo   1. WeChat is installed: config.yaml -^> wechat.exe_path
        echo   2. WeChat is logged in
        echo ============================================
        pause
        exit /b 1
    )
)
echo [OK] WeChat is ready

echo.
echo ============================================
echo   Startup complete
echo   Open: http://localhost:5000
echo ============================================
echo.

pause
