@echo off
rem 双击停止：先停后端，再给桥发 SIGINT（先提示托住机械臂）。
setlocal
chcp 65001 >nul
echo.
echo 停止演示
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop.ps1"
echo.
pause
