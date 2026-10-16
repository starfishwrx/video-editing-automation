# Existing schedulers may call this file; it never installs or enables a task.
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding
$configPath = Join-Path $env:LOCALAPPDATA 'JianyingNasTools\config.json'
if (-not (Test-Path -LiteralPath $configPath)) { Write-Host '未配置实际草稿库，同步已停止。'; exit 2 }
$config = Get-Content -Raw -Encoding UTF8 -LiteralPath $configPath | ConvertFrom-Json
if (-not $config.NasPackageRoot -or -not $config.DraftRoot) { Write-Host '同步配置不完整，已停止。'; exit 2 }
if (-not (Test-Path -LiteralPath $config.NasPackageRoot)) { Write-Host 'NAS 不可访问，本轮不复制。'; exit 2 }
if (-not (Test-Path -LiteralPath $config.DraftRoot)) { Write-Host '本地草稿库不可访问，已停止。'; exit 2 }
$failed = $false
foreach ($package in Get-ChildItem -LiteralPath $config.NasPackageRoot -Directory) {
    $manifestPath = Join-Path $package.FullName 'nas-package.json'
    if (-not (Test-Path -LiteralPath $manifestPath)) { continue }
    $manifest = Get-Content -Raw -Encoding UTF8 -LiteralPath $manifestPath | ConvertFrom-Json
    $name = [string]$manifest.draft_name
    if (-not $name -or [IO.Path]::GetFileName($name) -ne $name) { $failed = $true; continue }
    $target = Join-Path $config.DraftRoot $name
    if (Test-Path -LiteralPath $target) { continue }
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'nas-tool.ps1') -Action Import -Source $package.FullName -DraftRoot $config.DraftRoot -Destination $target
    if ($LASTEXITCODE -ne 0) { $failed = $true }
}
if ($failed) { exit 2 }
exit 0
