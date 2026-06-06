@echo off
chcp 65001 >nul
:: 订餐监控 - 静默启动

set "EXE=%~dp0订餐监控.exe"
set "LOCKFILE=%~dp0catering_monitor.lock"

:: 检查是否已在运行
if exist "%LOCKFILE%" (
    set /p PID=<"%LOCKFILE%"
    tasklist /fi "PID eq %PID%" 2>nul | find "%PID%" >nul
    if %errorlevel% equ 0 (
        echo 订餐监控已在运行中 (PID: %PID%)
        timeout /t 2 >nul
        exit /b
    ) else (
        del "%LOCKFILE%" 2>nul
    )
)

:: 启动
start "" "%EXE%"
echo 订餐监控已启动 (后台无窗口)
echo 13:00 自动开始 | 16:30 自动退出
timeout /t 3 >nul
