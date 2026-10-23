@echo off
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0fix_local.ps1"
if errorlevel 1 echo 本地化未通过检查，不代表修复成功。
pause
