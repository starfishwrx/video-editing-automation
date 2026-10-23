# Video automation candidate v1.5.6

2026-10-23 NAS 分发修订：正常出货与修复统一素材收集，复制核验后才发布目录，多版本归档使用独立媒体引用，新增双执行位置部署检查。见 [NAS 分发稳定性说明](docs/NAS_DELIVERY_STABILITY.md)。

2026-10-23 证据合同修订：关闭不完整 QC 响应与单包数量误放，统一重排后的证据复验、直接/关联排序和短动作搜索，接入私有人工审稿回放。行为与迁移见 [证据与反馈闭环说明](docs/QUALITY_CONTRACT_FIXES.md)。

2026-10-22 选镜审稿修订：回流开场展示、抽数/层数、揉搓与实验组句规则，修复去重抢镜、分数容差、Top3 与多镜旁路。行为与验收范围见 [选镜修订说明](docs/SELECTION_REVIEW_20261022.md)。

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
