#Requires -Version 5.1
param(
    [string]$PythonPath,
    [string]$WorkRoot,
    [string]$DraftRoot,
    [string]$QueuePath,
    [string]$ConfigPath,
    [switch]$DryRun
)
$ErrorActionPreference = 'Stop'
$packageRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$initialWork = if ($WorkRoot) { $WorkRoot } elseif ($env:JY_WORK_ROOT) { $env:JY_WORK_ROOT } else { Join-Path $packageRoot 'work' }
if (-not $ConfigPath) { $ConfigPath = Join-Path $initialWork 'config.local.ps1' }
if (Test-Path -LiteralPath $ConfigPath -PathType Leaf) {
    . $ConfigPath
} elseif ($PSBoundParameters.ContainsKey('ConfigPath')) {
    throw 'PIPELINE_CONFIG_NOT_FOUND'
}
if (-not $PythonPath) { $PythonPath = Join-Path $packageRoot '.venv/Scripts/python.exe' }
if (-not $WorkRoot) { $WorkRoot = if ($env:JY_WORK_ROOT) { $env:JY_WORK_ROOT } else { $initialWork } }
if (-not $DraftRoot) { $DraftRoot = $env:JY_DRAFT_ROOT }
if (-not $DraftRoot) { throw 'PIPELINE_DRAFT_ROOT_REQUIRED: supply -DraftRoot or local config' }
if (-not $QueuePath) { $QueuePath = Join-Path $WorkRoot 'batch_queue.json' }
if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) { throw 'PIPELINE_PYTHON_NOT_FOUND' }
$PythonPath = (Resolve-Path -LiteralPath $PythonPath).Path
$WorkRoot = [IO.Path]::GetFullPath($WorkRoot)
$DraftRoot = [IO.Path]::GetFullPath($DraftRoot)
$QueuePath = [IO.Path]::GetFullPath($QueuePath)
$env:JY_WORK_ROOT = $WorkRoot
$env:JY_PIPE_ROOT = $packageRoot
$env:JY_EVAL_RUNTIME_ROOT = $packageRoot
$env:JY_ORCHESTRATOR_ROOT = Join-Path $packageRoot 'doubao-jianying-orchestrator'
$env:JY_PYTHON_EXE = $PythonPath
$env:JY_DRAFT_ROOT = $DraftRoot
$env:JY_DRAFTS_ROOT = $DraftRoot
$env:JY_PROJECTS_ROOT = $DraftRoot
$env:PYTHONUTF8 = '1'
$arguments = @('-X', 'utf8', '-m', 'tools.workflow.scheduled', '--queue', $QueuePath, '--work-root', $WorkRoot)
if ($DryRun) { $arguments += '--dry-run' }
Push-Location -LiteralPath $packageRoot
try {
    & $PythonPath @arguments
    $pipelineExit = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $pipelineExit
