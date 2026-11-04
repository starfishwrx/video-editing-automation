# 快速开始（Windows / PowerShell）

从当前源码开始验证本地工具链，再准备真实素材和服务做预览。下面的合成视频步骤不调用视觉模型、TTS、剪映 GUI、NAS 或飞书。

## 1. 获取源码与依赖

需要 Git、[uv](https://docs.astral.sh/uv/getting-started/installation/) 和 Python 3.11 或 3.12。当前活代码目录名为 `video-v1.5.3`，实际版本从 `VERSION` 读取。

```powershell
git clone https://github.com/starfishwrx/video-editing-automation.git
cd video-editing-automation/packages/video-v1.5.3
uv sync --frozen
Get-Content VERSION
uv run --frozen python -X utf8 doubao-jianying-orchestrator/run.py --help
```

后续命令都在这个包根执行。不要切到实验包，也不要对系统 Python 安装依赖。

音色表和自动 BGM 曲库随仓库及源码包交付，无需从其他机器复制 `data/`。可离线运行 `run.py list-library --kw TVB` 检查名称；生产 `env` 会检查表是否缺失或损坏。元数据能查询不等于云端合成／下载已获授权或可用，详见[资源说明](../packages/video-v1.5.3/jianying-editor/resources/catalogs/README.md)。

## 2. 离线代码验证

```powershell
uv run --frozen python -X utf8 -m unittest discover -s tests -q
uv run --frozen python -X utf8 -m unittest discover -s doubao-jianying-orchestrator/tests -q
```

两条命令退出码都应为 0。单测中的模型响应使用模拟；测试成功不代表真实视觉准确率、服务可用或生产已部署。更多验收要求见[测试策略](test.md)。

## 3. 本机配置与抽帧示例

在独立 PowerShell 会话中操作。示例把私有配置、素材和草稿放在忽略的 `work/`，避免进入源码和发布包。

```powershell
New-Item -ItemType Directory -Force work/quickstart/media, work/quickstart/drafts | Out-Null
# 首次准备时复制；已有本机配置时保留并人工核对。
if (-not (Test-Path work/config.local.ps1)) {
    Copy-Item examples/config.example.ps1 work/config.local.ps1
}
. ./work/config.local.ps1
Copy-Item examples/manifest.preview.json work/quickstart/manifest.json
```

配置模板明确使用 **GLM-5.3-Flash** / `glm-5.3-flash`，不写入或覆盖密钥。运行时只支持 GLM（`volc_glm` / `glm` / `volc`）与 Qwen（`qwen` / `qwen_compatible`）直连，不需要启动额外的编码 Agent。使用 Qwen 时显式提供 `JY_QWEN_BASE_URL`、`JY_QWEN_MODEL` 和 `JY_QWEN_API_KEY`；旧通道及未知值直接报错。当前通用配置读取进程环境变量，`.env` 文件不会自动加载；文字复核、`JY_AI_EDITOR_QWEN` 与完整变量说明见[环境合同](../packages/video-v1.5.3/ENVIRONMENT.md)。

准备 FFmpeg。若已配置 `FFMPEG_BIN` / `FFPROBE_BIN`，会直接复用；否则 Windows 引导器下载便携工具到技能的 `bin/`（需要网络，不运行安装器）。

```powershell
$tools = uv run --frozen python -X utf8 doubao-jianying-orchestrator/run.py setup-ffmpeg | ConvertFrom-Json
if ($tools.status -notin @('READY', 'ALREADY_READY') -or -not $tools.ffprobe) {
    throw 'FFmpeg/ffprobe 未就绪；检查输出并配置 FFMPEG_BIN/FFPROBE_BIN。'
}
$env:FFMPEG_BIN = $tools.ffmpeg
$env:FFPROBE_BIN = $tools.ffprobe

& $env:FFMPEG_BIN -y -hide_banner -loglevel error -f lavfi `
  -i 'testsrc2=size=640x360:rate=25' -t 3 -c:v libx264 -pix_fmt yuv420p `
  work/quickstart/media/sample.mp4
if ($LASTEXITCODE -ne 0) { throw '合成视频失败。' }

uv run --frozen python -X utf8 doubao-jianying-orchestrator/run.py analyze-shots `
  --input work/quickstart/manifest.json --report-dir work/quickstart/extract-report --no-vision
if ($LASTEXITCODE -ne 0) { throw '抽帧失败；检查输出中的素材或探测问题。' }
```

检查 `work/quickstart/extract-report/shot_candidates.json`：`shots` 应非空，`pending_items` 不应含读帧/探测失败；`temporary_shot_frames/` 应有可读图片。无视觉调用时描述为空或保持待分析是正常结果。`SHOT_ANALYSIS_READY` 表示候选文件已生成，不是视觉认证。

合成图案只用于验证文件路径、解码、抽帧和 JSON。它不提供商品卖点或识图效果的验证素材。再次演示时使用新的 report 目录以保留之前的结果。

## 4. 真实素材的视觉分析与预览建稿

先把 `work/quickstart/manifest.json` 的 `product`、`category`、`material_sources`、逐句 `narration` 替换成真实同 SKU 任务。相对素材路径以 manifest 所在目录为基准；保留 `selection_mode=ai_editor`。需要指定音色时按[音频说明](subsystems/audio-voice.md)填写 `voice`，并准备对应 TTS 运行环境。

通过本机安全配置为进程提供 Ark 凭据：优先 `ARK_API_KEY`，其次 `JY_VOLC_API_KEY`。本地环境检查不验证云端密钥。下面的视觉分析/建稿会发送实际帧与脚本文字并使用所配置服务的额度。

```powershell
uv run --frozen python -X utf8 doubao-jianying-orchestrator/run.py env --profile base
# 此处仅分析素材；base检查通用工具链，不声明无头建稿已就绪。
uv run --frozen python -X utf8 doubao-jianying-orchestrator/run.py analyze-shots `
  --input work/quickstart/manifest.json --report-dir work/quickstart/vision-report
```

核对 `shot_candidates.json` 的 `vision_analysis.errors`、模型摘要和各镜头观察。命令退出码或 `SHOT_ANALYSIS_READY` 本身不保证视觉成功，未知项需要先处理。

建稿前，按环境合同准备已授权的独立无头后端，配置 `JIANYING_HEADLESS_ROOT`、匹配的 `JIANYING_INSTALL_DIR` 和静态字体 `JY_HEADLESS_FONT`（或任务的 `subtitle_font_path`）；草稿库放在剪映首页库之外。

新机器或新的 Windows 运行账号还需要完成剪映首次初始化：用实际运行流水线的同一用户启动已安装的剪映，完成首次设置，让剪映自行建立默认本地草稿目录和首页索引。仅创建独立输出目录不能替代这一步。若预检返回 `HEADLESS_HOST_NOT_INITIALIZED`，先完成初始化再重跑；不要手工创建、复制或覆盖 `root_meta_info.json`，也不要借用其他账号的索引。此要求来自当前原生后端的宿主检查，不代表流水线会向首页登记草稿。

先运行默认生产环境检查，确认通过后再调用配音或视觉服务：

```powershell
New-Item -ItemType Directory -Force $env:JY_DRAFT_ROOT | Out-Null
uv run --frozen python -X utf8 doubao-jianying-orchestrator/run.py env --input work/quickstart/manifest.json
if ($LASTEXITCODE -ne 0) { throw '请先处理生产环境报告中的阻断项。' }
```

`env` 默认检查基础工具链与实际所选写稿后端，未就绪返回非零退出码；基础环境和原生后端检查分别列出。它不认证云端密钥，正式build也会在配音/视觉开销前复验。

```powershell
uv run --frozen python -X utf8 doubao-jianying-orchestrator/run.py build `
  --input work/quickstart/manifest.json --report-dir work/quickstart/build-attempt-01 `
  --delivery-mode preview
```

`build` 的 `--delivery-mode` 决定该次模式，默认是 formal；仅在 manifest 写 preview 不足以替代这个参数。根据输出的 `status` / `next_action` 处理音色确认、环境、模型或素材阻断。成功时按返回路径打开草稿，并查看 `shot_match_report.json`、`ai_editor/plan.json` 和任务结果。每次尝试使用新 report 目录。

独立视觉 QC、真实剪映打开与人工审阅分别执行，见[视觉子系统](subsystems/visual-matching.md)和[运行时合同](../packages/video-v1.5.3/doubao-jianying-orchestrator/references/runtime_contract.md)。预览保持未认证，不会因为生成了草稿就成为正式交付。

## 5. 接入生产前

整个项目的手动与定时运行入口见 [Windows 脚本流水线](windows-pipeline.md)。生产队列、NAS 和飞书使用本机配置；Mac 交接已删除。历史 JyPoll/JyRun 入口（`jy_poll.ps1` / `run_task.template.ps1`）已退役，调用会立即失败；单条任务继续使用上述 manifest CLI，已有批队列使用 `scripts/run_pipeline.ps1` 运行一轮，内部直接调用 dispatcher。源码更新不会自动启用队列、修改计划任务或替换已有生产生成脚本。按照[发布流程](release-process.md)和既定真实任务观察完成验收后再部署，保留当前队列和回滚依据。
