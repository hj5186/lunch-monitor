@echo off
chcp 65001 >nul
:: 订餐监控 - 精确停止 (只杀本脚本进程, 不影响其他 Python 程序)

set "LOCKFILE=%~dp0catering_monitor.lock"

if not exist "%LOCKFILE%" (
    echo [未找到运行中的订餐监控进程]
    timeout /t 2 >nul
    exit /b
)

:: 从锁文件读取 PID
set /p PID=<"%LOCKFILE%"
echo 订餐监控 PID: %PID%

:: 只杀这个 PID
taskkill /f /pid %PID% 2>nul
if %errorlevel% equ 0 (
    echo 已停止订餐监控
) else (
    echo 进程可能已自行退出
)

:: 清理锁文件
del "%LOCKFILE%" 2>nul
timeout /t 2 >nul
