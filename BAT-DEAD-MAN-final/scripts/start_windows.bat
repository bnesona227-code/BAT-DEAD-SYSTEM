@echo off
chcp 65001 >nul
cd /d "%~dp0.."
echo =================================================================
echo  BAT-DEAD-MAN // WINDOWS LAUNCHER (100%% ZERO-PIP)
echo =================================================================
where python >nul 2>nul
if %errorlevel% neq 0 (
    echo [ERROR] Python не найден! Установите Python 3.8+ с python.org.
    pause
    exit /b 1
)
echo [INFO] Открываю локальный пульт управления: http://127.0.0.1:8080
start "" "http://127.0.0.1:8080"
python src\server.py
pause
