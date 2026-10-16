$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding
$python = Get-Command python.exe -ErrorAction SilentlyContinue
$launcher = @()
if (-not $python) {
    $python = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($python) { $launcher = @('-3') }
}
if (-not $python) { Write-Host '未找到 Python 3，已停止。'; exit 2 }
$module = Join-Path $PSScriptRoot 'nas_portable.py'
if (-not (Test-Path -LiteralPath $module)) { Write-Host '本地化工具不完整，已停止。'; exit 2 }
& $python.Source @launcher -X utf8 $module localize --source $PSScriptRoot
exit $LASTEXITCODE
