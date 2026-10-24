# Copy to work/config.local.ps1, then dot-source in the same PowerShell session.
# Keep the copy directly under work/ so the package-relative paths stay valid.
# This template configures an isolated local preview, not a production queue.
$examplePackageRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$exampleDraftRoot = Join-Path $examplePackageRoot 'work/quickstart/drafts'

$env:JY_SKILL_ROOT = $examplePackageRoot
$env:JY_DRAFT_ROOT = $exampleDraftRoot
$env:JY_DRAFTS_ROOT = $exampleDraftRoot
$env:JY_PROJECTS_ROOT = $exampleDraftRoot
$env:JY_SELECTION_MODE = 'ai_editor'
$env:JY_VISION_TRANSPORT = 'volc_glm'
$env:JY_VISION_MODEL = 'glm-5.3-flash'
$env:JY_VOLC_BASE_URL = 'https://ark.cn-beijing.volces.com/api/plan/v3'
$env:JY_EVAL_MODE = 'observe'

# Supply ARK_API_KEY (or JY_VOLC_API_KEY) through local process configuration.
# No credential is assigned here, so an injected key is not overwritten.
# Optional executable paths: FFMPEG_BIN and FFPROBE_BIN.
# Vision worker/slot defaults are 20; use JY_VISION_BATCH_WORKERS and
# JY_VISION_SLOTS to set limits suitable for your service and workstation.
