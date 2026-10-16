param(
    [ValidateSet('Import','Export','Inspect')][string]$Action = 'Import',
    [string]$Source,
    [string]$DraftRoot,
    [string]$Destination
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding
try {
    $config = @{}
    $configPath = Join-Path $env:LOCALAPPDATA 'JianyingNasTools\config.json'
    if (Test-Path -LiteralPath $configPath) {
        $config = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
    }
    if (-not $Source) {
        if (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'draft_info.json')) { $Source = $PSScriptRoot }
        else { throw '请把草稿或 NAS 草稿包文件夹拖到对应的取稿/上传入口。' }
    }
    $Source = (Get-Item -LiteralPath $Source).FullName
    $python = Get-Command python.exe -ErrorAction SilentlyContinue
    $launcher = @()
    if (-not $python) {
        $python = Get-Command py.exe -ErrorAction SilentlyContinue
        if ($python) { $launcher = @('-3') }
    }
    if (-not $python) { throw '没有找到 Python 3；本工具不会自动安装软件。' }
    $module = Join-Path $PSScriptRoot 'nas_portable.py'
    if (-not (Test-Path -LiteralPath $module)) { throw '工具文件不完整，缺少 nas_portable.py。' }
    if ($Action -eq 'Inspect') {
        & $python.Source @launcher -X utf8 $module inspect --source $Source
        if ($LASTEXITCODE -ne 0) { throw '草稿不能安全验证，未修改或覆盖任何草稿。' }
        exit 0
    }
    if ($Action -eq 'Export') {
        if (-not $Destination) {
            $packageRoot = $config.NasPackageRoot
            if (-not $packageRoot -and $env:JY_NAS_SHARE) { $packageRoot = Join-Path $env:JY_NAS_SHARE '测试产出\可移植草稿' }
            if (-not $packageRoot) { throw '未配置 NAS 上传目录，请填写本机工具配置或传入 Destination。' }
            $name = (Split-Path -Leaf $Source) + '-' + (Get-Date -Format 'yyyyMMdd_HHmmss')
            $Destination = Join-Path $packageRoot $name
        }
        & $python.Source @launcher -X utf8 $module export --source $Source --destination $Destination
        if ($LASTEXITCODE -ne 0) { throw '上传检查失败，未发布可取稿包；请查看上面的具体原因。' }
    } else {
        if (-not $DraftRoot) { $DraftRoot = $env:JY_DRAFT_ROOT }
        if (-not $DraftRoot) { $DraftRoot = $config.DraftRoot }
        if (-not $DraftRoot) { throw '未配置实际剪映草稿目录，已停止；不会猜测默认目录。' }
        if (-not (Test-Path -LiteralPath $DraftRoot -PathType Container)) { throw '配置的剪映草稿目录不存在。' }
        $originalSource = $Source
        $temporary = $null
        try {
            if (-not (Test-Path -LiteralPath (Join-Path $Source 'nas-package.json'))) {
                # Legacy readable bundles may be collected and certified first.
                # Encrypted bundles stop here instead of patching stale records.
                $temporary = Join-Path ([IO.Path]::GetTempPath()) ('jy-nas-' + [guid]::NewGuid().ToString('N'))
                $Source = Join-Path $temporary 'package'
                & $python.Source @launcher -X utf8 $module export --source $originalSource --destination $Source
                if ($LASTEXITCODE -ne 0) { throw '旧包无法安全取稿，未改用旧记录替代加密时间轴。' }
            }
            if (-not $Destination) {
                $manifest = Get-Content -LiteralPath (Join-Path $Source 'nas-package.json') -Raw -Encoding UTF8 | ConvertFrom-Json
                $name = [string]$manifest.draft_name
                if ([IO.Path]::GetFileName($name) -ne $name -or -not $name) { throw '包中的草稿名称不合法。' }
                $Destination = Join-Path $DraftRoot $name
                if (Test-Path -LiteralPath $Destination) {
                    $Destination = Join-Path $DraftRoot ($name + '-取稿-' + (Get-Date -Format 'yyyyMMdd_HHmmss'))
                }
            }
            & $python.Source @launcher -X utf8 $module import --source $Source --destination $Destination
            if ($LASTEXITCODE -ne 0) { throw '取稿验证失败，没有覆盖本地编辑稿。' }
        } finally {
            # Keep temporary evidence for diagnostics; no broad deletes.
        }
    }
    Write-Host ('文件检查完成，位置：' + $Destination)
    Write-Host '已验证素材复制和路径；打开剪映检查画面后再进行正式交付。'
    exit 0
} catch {
    Write-Host ('已停止：' + $_.Exception.Message) -ForegroundColor Red
    exit 2
}
