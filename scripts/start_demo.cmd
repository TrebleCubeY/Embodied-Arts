@echo off
rem 双击这个文件就是「一键起」。
rem 参数照样能传：start_demo.cmd -SideBySide
rem   -Mode hold|teleop|keyframes   起手档位
rem   -SideBySide                   本机单屏时两个窗口并排
rem   -NoRobot                      不接机械臂，只演屏幕
rem   -ListScreens                  只看屏幕编号
setlocal
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0launch.ps1" %*
echo.
pause
