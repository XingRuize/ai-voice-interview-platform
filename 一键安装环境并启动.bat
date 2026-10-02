@echo off
chcp 65001 >nul
cd /d "%~dp0"
title AI Voice Interview - Installer
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\bootstrap_windows.ps1"
if errorlevel 1 (
  echo.
  echo 安装或启动没有完成。请查看上方提示和 install.log。
  pause
  exit /b 1
)
echo.
echo 安装完成，访谈平台已经启动。
pause
