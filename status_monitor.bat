@echo off
chcp 65001 >nul
:: 订餐监控 - 状态查询

set "LOCKFILE=%~dp0catering_monitor.lock"
set "LOGFILE=%~dp0catering_monitor.log"

echo ========================================
echo        订餐监控 - 状态查询
echo ========================================
echo.

if not exist "%LOCKFILE%" (
    echo [状态] 未运行
    echo.
    echo 启动方式: 双击 start_monitor.bat
) else (
    set /p PID=<"%LOCKFILE%"
    tasklist /fi "PID eq %PID%" 2>nul | find "%PID%" >nul
    if %errorlevel% equ 0 (
        echo [状态] 运行中 (PID: %PID%)
    ) else (
        echo [状态] 锁文件残留 (上次异常退出)
        del "%LOCKFILE%" 2>nul
        echo         已自动清理, 可以重新启动
    )
)

echo.
echo 启动方式: 双击 start_monitor.bat
echo 停止方式: 双击 stop_monitor.bat

:: 显示今日告警记录
if exist "catering_monitor_state.json" (
    echo.
    echo --- 今日告警记录 ---
    type "%~dp0catering_monitor_state.json" 2>nul
)

echo.
pause
