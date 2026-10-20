#Requires -Version 5.1
<#
.SYNOPSIS
  Sync Python deps for video-v1.5.3 with uv (never pip).
#>
$ErrorActionPreference = "Stop"
$uv = Join-Path $env:USERPROFILE ".local\bin\uv.exe"
if (-not (Test-Path $uv)) {
  Write-Host "Installing uv..."
  powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
}
$env:Path = "$(Join-Path $env:USERPROFILE '.local\bin');$env:Path"
$root = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
if (-not (Test-Path (Join-Path $PSScriptRoot "..\pyproject.toml"))) {
  $root = Resolve-Path (Join-Path $PSScriptRoot "..")
} else {
  $root = Resolve-Path (Join-Path $PSScriptRoot "..")
}
Set-Location $root
Write-Host "uv sync in $root"
& uv sync
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& uv run python -c "from importlib.util import find_spec; assert find_spec('pymediainfo'); print('uv env ok', __import__('sys').executable)"
exit $LASTEXITCODE
