#Requires -Version 5.1
param(
    [string]$PythonPath,
    [string]$WorkRoot,
    [string]$DraftRoot,
    [string]$QueuePath,
    [string]$ConfigPath,
    [string]$TaskName = 'VideoAutomationPipeline',
    [ValidateRange(1, 1440)][int]$IntervalMinutes = 5
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
if (-not $DraftRoot) { throw 'PIPELINE_DRAFT_ROOT_REQUIRED' }
if (-not $QueuePath) { $QueuePath = Join-Path $WorkRoot 'batch_queue.json' }
if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) { throw 'PIPELINE_PYTHON_NOT_FOUND' }
$PythonPath = (Resolve-Path -LiteralPath $PythonPath).Path
$WorkRoot = [IO.Path]::GetFullPath($WorkRoot)
$DraftRoot = [IO.Path]::GetFullPath($DraftRoot)
$QueuePath = [IO.Path]::GetFullPath($QueuePath)
$ConfigPath = [IO.Path]::GetFullPath($ConfigPath)
function Quote-TaskArgument([string]$Value) {
    # Windows paths cannot contain quotes; escape the trailing backslashes
    # before adding the closing command-line quote (including drive roots).
    if ($Value.Contains('"') -or $Value.Contains("`r") -or $Value.Contains("`n")) { throw 'INVALID_TASK_ARGUMENT' }
    return '"' + [regex]::Replace($Value, '(\\+)$', '$1$1') + '"'
}
$runner = Join-Path $PSScriptRoot 'run_pipeline.ps1'
$arguments = @('-NoProfile', '-NonInteractive', '-WindowStyle', 'Hidden', '-ExecutionPolicy', 'Bypass', '-File', (Quote-TaskArgument $runner),
    '-PythonPath', (Quote-TaskArgument $PythonPath), '-WorkRoot', (Quote-TaskArgument $WorkRoot),
    '-DraftRoot', (Quote-TaskArgument $DraftRoot), '-QueuePath', (Quote-TaskArgument $QueuePath))
if (Test-Path -LiteralPath $ConfigPath -PathType Leaf) { $arguments += @('-ConfigPath', (Quote-TaskArgument $ConfigPath)) }
$powershell = Join-Path $env:SystemRoot 'System32/WindowsPowerShell/v1.0/powershell.exe'
$action = New-ScheduledTaskAction -Execute $powershell -Argument ($arguments -join ' ') -WorkingDirectory $packageRoot
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
# Register only when this script is explicitly invoked; never starts the task.
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force
