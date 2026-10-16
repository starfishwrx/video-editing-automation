@echo off
chcp 65001 >nul
if not exist "%LOCALAPPDATA%\JianyingNasTools\config.json" (
  echo 请先配置实际草稿库和 NAS 包目录。未创建自动同步任务。
  pause
  exit /b 2
)
set "DIR=%LOCALAPPDATA%\JianyingDraftSync"
if not exist "%DIR%" mkdir "%DIR%"
copy /y "%~dp0sync.ps1" "%DIR%\" >nul
copy /y "%~dp0fix_local.ps1" "%DIR%\" >nul
copy /y "%~dp0nas_portable.py" "%DIR%\" >nul
copy /y "%~dp0nas-tool.ps1" "%DIR%\" >nul
if not exist "%DIR%\nas_portable.py" exit /b 2
echo 由本次手动安装注册每十分钟的校验取稿任务。
schtasks /create /f /tn "JianyingDraftSync" /tr "powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File \"%DIR%\sync.ps1\"" /sc minute /mo 10 >nul 2>nul
if errorlevel 1 (
  echo 计划任务创建失败。
  pause
  exit /b 1
)
schtasks /run /tn "JianyingDraftSync" >nul 2>nul
echo 安装完成。只同步通过文件校验的可移植包，不覆盖已有编辑稿。
echo 打开剪映检查画面和声音后，再进行正式交付。
pause
