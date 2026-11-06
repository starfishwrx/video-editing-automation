# Copy to work/config.local.ps1, then dot-source in the same PowerShell session.
# Keep the copy directly under work/ so the package-relative paths stay valid.
# This template configures an isolated local preview, not a production queue.
$examplePackageRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$exampleDraftRoot = Join-Path $examplePackageRoot 'work/quickstart/drafts'

$env:JY_SKILL_ROOT = Join-Path $examplePackageRoot 'jianying-editor'
$env:JY_CONFIG_SOURCE = $PSCommandPath
$env:JY_DRAFT_ROOT = $exampleDraftRoot
$env:JY_DRAFTS_ROOT = $exampleDraftRoot
$env:JY_PROJECTS_ROOT = $exampleDraftRoot
# The application defaults to ai_editor. An existing JY_SELECTION_MODE remains
# visible and is reported by the task instead of being silently overwritten.
$env:JY_DRAFT_BACKEND = 'headless'
# Configure the separately installed, verified headless project and native font.
# Set JIANYING_HEADLESS_ROOT, JIANYING_INSTALL_DIR and JY_HEADLESS_FONT locally.
# Keep JY_DRAFT_ROOT outside the editor's live home library; no UI index is used.
$env:JY_VISION_TRANSPORT = 'volc_glm'
$env:JY_VISION_MODEL = 'glm-5.3-flash'
$env:JY_VOLC_BASE_URL = 'https://ark.cn-beijing.volces.com/api/plan/v3'

# Supply ARK_API_KEY (or JY_VOLC_API_KEY) through local process configuration.
# No credential is assigned here, so an injected key is not overwritten.
# Optional executable paths: FFMPEG_BIN and FFPROBE_BIN.
# Vision worker/slot defaults are 20; use JY_VISION_BATCH_WORKERS and
# JY_VISION_SLOTS to set limits suitable for your service and workstation.

# Initial workstation limits; explicit concurrency policy, if set, takes precedence.
$env:JY_TASK_WORKERS = '3'
$env:JY_BUILD_TIMEOUT_S = '7200'
$env:JY_TASK_BUDGET_S = '14400'
