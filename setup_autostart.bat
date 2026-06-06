@echo off
chcp 65001 >nul
:: 开机自启 - 设置脚本

set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "MONITOR_DIR=%~dp0"

echo ========================================
echo     订餐监控 - 开机自启设置
echo ========================================
echo.

set "SHORTCUT=%STARTUP%\订餐监控.lnk"

echo 正在创建开机自启快捷方式...
powershell -Command "$s=(New-Object -COM WScript.Shell).CreateShortcut('%SHORTCUT%');$s.TargetPath='%MONITOR_DIR%订餐监控.exe';$s.WorkingDirectory='%MONITOR_DIR%';$s.WindowStyle=7;$s.Description='订餐监控闹钟';$s.Save()"

if exist "%SHORTCUT%" (
    echo [完成] 已设置开机自启
    echo         每次开机自动静默启动
) else (
    echo [失败] 无法创建快捷方式
)

echo.
echo 取消自启: 删除此文件即可
echo     %SHORTCUT%
echo.
pause
