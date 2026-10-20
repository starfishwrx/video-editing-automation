# Video automation v1.5.4 experimental

这是隔离的本地测试包。新增内容指纹素材库、SigLIP 2 本地粗检索、按需视觉核验、整稿连续动作规划，以及实际切片独立审核；继承 1.5.3 的草稿交付修复和质量检查。默认旧 manifest 继续使用 `legacy` 路径，测试启动器明确选择 `indexed_contextual`。源码、ZIP、已安装技能和生产部署分别验收，本包不会自动替换现有生产。

完整实现与验收边界见 [1.5.4 更新计划](docs/V154_UPDATE_PLAN.md)。模型权重、素材、索引数据库、任务日志与业务凭据不包含在 ZIP 中。首次下载模型和冷入库单独计时，后续复用不应重复素材理解。

## 隔离试跑

在本包根目录执行，五个路径参数都必须显式填写；四个输出目录相互分离，并放在包目录及生产草稿库之外。

```powershell
uv sync --frozen --group indexing

# $manifestPath 是原任务 manifest；$testRoot 是独立的本地测试目录。
# 已冻结的逐句音频文件直接复用；新任务通过正常入口合成并冻结真实时长。
uv run --frozen --group indexing python -X utf8 scripts/run_v154_test.py `
  --input $manifestPath `
  --report-dir "$testRoot/report" `
  --index-dir "$testRoot/index" `
  --model-cache-dir "$testRoot/models" `
  --drafts-root "$testRoot/drafts" `
  --dry-run
```

核对打印的路径后去掉 `--dry-run` 进行本地测试。启动器只复制 manifest 并为子进程注入路径，调用包内正常 `run.py build`；不修改输入、注册表、全局环境或已安装技能。GPU 本地检索批量默认 8，显存不足缩小批量；媒体编解码继续使用既有资源槽位和硬件回退。

同一索引也可通过正常 CLI 单独建立，`index-materials` 不调用远端视觉：

```powershell
uv run --frozen --group indexing python doubao-jianying-orchestrator/run.py index-materials `
  --input $manifestPath --report-dir "$testRoot/index-report" `
  --index-dir "$testRoot/index" --model-cache-dir "$testRoot/models"
```

只有经过观察证据核验的候选可以建稿；素材相关度高、程序测试通过或生成成功都不能代替成片与人工审核。缺证、截断动作或时长无解时保留具体句组缺口，不用包装、动作结束定住或黑屏冒充功能画面。所有试跑保持 `formal_delivery_allowed=false`、人工审核 `PENDING`，不自动部署、发 PR、写 NAS 或回填飞书。

可选 AGY CLI 图像通道默认 **20 并发**，候选观察调度和跨进程槽位共同限流；纯文字规划及其他视觉通道仍沿用原配置。配置见 [运行环境](ENVIRONMENT.md#optional-antigravity-cli-image-transport)。本机两次 Gemini 3.8 Flash 实测均被服务以地区不支持拒绝，因此尚未完成真实视觉效果或吞吐验证；并发机制使用本地模拟 CLI 验证。

## 继承的 1.5.3 功能

2026-10-13 至 10-16 的 Codex 审核与本机实现对照见 [这四天更新说明](docs/UPDATES_20261013-16.md)。

草稿交付修复已合入候选源码：默认 UNC 分发新增实际媒体路径验收；历史加密稿可在用户确认后恢复原始生成版。
用法与验收边界见 [草稿分发与生成版恢复](doubao-jianying-orchestrator/references/draft_delivery_and_recovery.md)。
源码合包不等于安装：部署前回读实际运行版本，保留正在运行的队列。

[已验证修复合包技能](doubao-jianying-orchestrator/references/validated_fix_to_release.md) 与
`tools.release_package` 一起随包提供。局部迭代用 `--checks delivery`，发版用 `--checks full`；
生成清单、ZIP、全新解压检查和测试收据不再依赖本机临时脚本。

Default Eval mode is observation. Visual policy remains independently versioned
at 1.4.1. Do not deploy over a running queue or publish release tags before the
two-real-task rollout gate passes. See the bundled orchestrator SKILL and
`references/runtime_contract.md` for the evidence contract.

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

# 在包根按锁文件同步依赖（写入 .venv）
cd packages/video-v1.5.4
uv sync --frozen --group indexing
uv run --frozen --group indexing python -X utf8 -m unittest discover -s doubao-jianying-orchestrator/tests -q
uv run --frozen --group indexing python -X utf8 -m unittest discover -s tests -q

# 打包并在解压副本验收（$releaseOutput 是包外尚不存在的目录）
uv run --frozen --group indexing python -X utf8 -m tools.release_package build --output $releaseOutput --checks full
```

缺包时用 `uv add <name>`，不要手改 site-packages。依赖声明见根目录 `pyproject.toml`。

卫品历史踩坑（1.5.2）：见 `docs/WEIPIN_1.5.2_PITFALLS.md`。正式质量合同见 `doubao-jianying-orchestrator/references/production_quality_and_lessons.md`。
