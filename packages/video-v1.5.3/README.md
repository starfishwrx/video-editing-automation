# Video automation candidate v1.5.3

2026-10-13 至 10-16 的 Codex 审核与本机实现对照见 [这四天更新说明](docs/UPDATES_20261013-16.md)。

草稿交付修复已合入候选源码：默认 UNC 分发新增实际媒体路径验收；历史加密稿可在用户确认后恢复原始生成版。
用法与验收边界见 [草稿分发与生成版恢复](doubao-jianying-orchestrator/references/draft_delivery_and_recovery.md)。
源码合包不等于安装：部署前回读实际运行版本，保留正在运行的队列。

Default Eval mode is observation. Visual policy remains independently versioned
at 1.4.1. Do not deploy over a running queue or publish release tags before the
two-real-task rollout gate passes. See ../../docs/video-eval-v1.5.0.md.

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

本包与宿主 `D:\jy_pipeline` **统一用 [uv](https://docs.astral.sh/uv/) 管理依赖**，不要 `pip install`。

```powershell
# 安装 uv（仅一次）
powershell -ExecutionPolicy Bypass -c "irm https://astral.sh/uv/install.ps1 | iex"
$env:Path = "$env:USERPROFILE\.local\bin;$env:Path"

# 在包根同步依赖（写入 .venv + uv.lock）
cd D:\jy_pipeline\work\fork-video-editing-automation\packages\video-v1.5.3
uv sync
uv run --frozen python -X utf8 -m unittest discover -s doubao-jianying-orchestrator/tests -q
uv run --frozen python -X utf8 -m unittest discover -s tests -q

# 宿主批处理脚本同样：
cd D:\jy_pipeline
uv sync
uv run python run_batch_weipin_20261014.py --help
```

缺包时用 `uv add <name>`，不要手改 site-packages。依赖声明见根目录 `pyproject.toml`。

卫品历史踩坑（1.5.2）：见 `docs/WEIPIN_1.5.2_PITFALLS.md`。正式质量合同见 `doubao-jianying-orchestrator/references/production_quality_and_lessons.md`。
