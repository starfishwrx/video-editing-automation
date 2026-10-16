@echo off
chcp 65001 >nul
set "SOURCE=%~1"
if not defined SOURCE set "SOURCE=%~dp0"
if "%SOURCE:~-1%"=="\" set "SOURCE=%SOURCE:~0,-1%"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0nas-tool.ps1" -Action Import -Source "%SOURCE%"
if errorlevel 1 echo 取稿已停止，请查看具体原因。本地编辑稿保持原样。
pause
