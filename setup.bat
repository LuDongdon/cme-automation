@echo off
chcp 65001 >nul
echo ============================================
echo   继续教育自动化工具 — 安装依赖
echo ============================================
echo.

cd /d "%~dp0"

echo [1/3] 安装 Python 依赖...
pip install -r requirements.txt -q
if %errorlevel% neq 0 (
    echo 依赖安装失败，请检查网络连接
    pause
    exit /b 1
)

echo [2/3] 安装 Playwright 浏览器...
python -m playwright install chromium
if %errorlevel% neq 0 (
    echo 浏览器安装失败
    pause
    exit /b 1
)

echo [3/3] 创建必要目录...
if not exist "data" mkdir data
if not exist "screenshots" mkdir screenshots
if not exist "logs" mkdir logs

echo.
echo ============================================
echo   安装完成！双击 start.bat 启动
echo ============================================
pause
