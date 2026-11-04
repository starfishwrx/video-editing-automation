# Video automation candidate v1.6.0

默认视觉模型为 **GLM-5.3-Flash**（`glm-5.3-flash`），通过火山 Ark 直接发送图片；请保留完整型号，不与 GLM-5.3 或 ChatGLM3 混写。运行时只支持 GLM（`volc_glm` / `glm` / `volc`）与 Qwen（`qwen` / `qwen_compatible`）直连；Codex CLI、Anthropic 和 CCSwitch 调用已移除，旧通道和未知值明确拒绝。默认 `ai_editor` 复审候选并整稿编排，最终 QC 为 v4；`rule_then_ai` 与显式 `legacy` 算法继续通过上述直连通道执行。文字复核与 `JY_AI_EDITOR_QWEN` 路由保留，详见 [ENVIRONMENT.md](ENVIRONMENT.md)。

1.6.0 默认无头建稿，BGM 为空自动选曲，显式曲目优先；独立QC、NAS分发与归档读取原生活动时间轴，正式四项门禁保留。见 [ENVIRONMENT.md](ENVIRONMENT.md) 的后端、字体与草稿库配置。

整个项目按纯 Windows 脚本运行：`scripts/run_pipeline.ps1` 手动执行一轮，`scripts/install_pipeline_task.ps1` 显式注册定时执行；内部直接调用模块和 GLM/Qwen API。Mac 交接和人工 prepare/collect 已删除。

新用户从仓库[快速开始](https://github.com/starfishwrx/video-editing-automation/blob/main/docs/quickstart.md)、[模型说明](https://github.com/starfishwrx/video-editing-automation/blob/main/docs/model-and-vision.md)及本包 [ENVIRONMENT.md](ENVIRONMENT.md) 进入。包内 `examples/` 提供脱敏 manifest 和 PowerShell 配置模板。源码候选不代表 GitHub 已发布该版本，下载状态见仓库 README。

2026-10-23 NAS 分发修订：正常出货与修复统一素材收集，复制核验后才发布目录，多版本归档使用独立媒体引用，新增双执行位置部署检查。见 [NAS 分发稳定性说明](docs/NAS_DELIVERY_STABILITY.md)。

2026-10-23 证据合同修订：关闭不完整 QC 响应与单包数量误放，统一重排后的证据复验、直接/关联排序和短动作搜索，接入私有人工审稿回放。行为与迁移见 [证据与反馈闭环说明](docs/QUALITY_CONTRACT_FIXES.md)。

2026-10-22 选镜审稿修订：回流开场展示、抽数/层数、揉搓与实验组句规则，修复去重抢镜、分数容差、Top3 与多镜旁路。行为与验收范围见 [选镜修订说明](docs/SELECTION_REVIEW_20261022.md)。

2026-10-13 至 10-16 的 Codex 审核与本机实现对照见 [这四天更新说明](docs/UPDATES_20261013-16.md)。

草稿交付修复已合入候选源码：默认 UNC 分发新增实际媒体路径验收；历史加密稿可在用户确认后恢复原始生成版。
用法与验收边界见 [草稿分发与生成版恢复](doubao-jianying-orchestrator/references/draft_delivery_and_recovery.md)。
源码合包不等于安装：部署前回读实际运行版本，保留正在运行的队列。历史 JyPoll/JyRun 执行链已退役，`jy_poll.ps1` 与 `run_task.template.ps1` 调用即失败；单任务、批量和定时使用 `scripts/run_pipeline.ps1`；单任务指定 `-RecordId`。底层 manifest CLI 不单独承担独立 QC 和交付闭环。源码更新不修改本机计划任务和既有生产生成脚本，也不自动启用队列。

[已验证修复合包技能](doubao-jianying-orchestrator/references/validated_fix_to_release.md) 与
`tools.release_package` 一起随包提供。局部迭代用 `--checks delivery`，发版用 `--checks full`；
生成清单、ZIP、全新解压检查和测试收据不再依赖本机临时脚本。

Automatic acceptance always verifies input, runtime, draft package and independent visual QC. Preview and unknown evidence cannot pass formal delivery. Visual policy remains independently versioned at 1.4.1. Runtime identity binds current package/backend bytes and policy metadata; it does not certify finished pixels.

This is the reusable video-automation capability extracted from the latest
local video package. It contains the matcher, timing contract, Jianying draft
writer, closed-loop gates, and regression tests.

The package intentionally excludes media, credentials, production logs,
machine-specific paths, and local knowledge-base content. Runtime deployment
must inject the environment variables documented in `ENVIRONMENT.md`.

The visual governance policy is a separately deployable business layer. This
package consumes its version and evidence contract; it does not copy business
rules into Diaodu or RDM.

## Python 依赖（只用 UV）

本包与部署宿主**统一用 [uv](https://docs.astral.sh/uv/) 管理依赖**，不要 `pip install`。

```powershell
# 安装 uv（仅一次）
powershell -ExecutionPolicy Bypass -c "irm https://astral.sh/uv/install.ps1 | iex"
$env:Path = "$env:USERPROFILE\.local\bin;$env:Path"

# 在包根同步依赖（写入 .venv + uv.lock）
cd packages/video-v1.5.3
uv sync --frozen
uv run --frozen python -X utf8 -m unittest discover -s doubao-jianying-orchestrator/tests -q
uv run --frozen python -X utf8 -m unittest discover -s tests -q

# 打包并在解压副本验收（$releaseOutput 是包外尚不存在的目录）
uv run --frozen python -X utf8 -m tools.release_package build --output $releaseOutput --checks full
```

缺包时用 `uv add <name>`，不要手改 site-packages。依赖声明见根目录 `pyproject.toml`。

卫品历史踩坑（1.5.2）：见 `docs/WEIPIN_1.5.2_PITFALLS.md`。正式质量合同见 `doubao-jianying-orchestrator/references/production_quality_and_lessons.md`。
