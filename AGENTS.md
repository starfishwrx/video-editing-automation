# AGENTS.md — 路由表（给 Agent 看的第一份文件）

> 本文件只做指路。产品目标见 `docs/prd.md`，架构定案见 `docs/rfc.md`，按日记录见 `docs/working.md`，验证完成标准见 `docs/test.md`。新坑写进 `LESSONS.md`，并在当天 `docs/working.md` Changelog 留一条。

## 0. 开始前先读

1. **版本身份**：可改的候选包目录是 `packages/video-v1.5.3/`（目录名是历史代号，不随小版本改名）。真实版本只读该目录下的 `VERSION`（当前主线 **1.5.6**）。生产以部署机器的 `ACTIVE_VIDEO_RUNTIME.json` 与实际安装文件为准。开发使用从 `main` 创建的干净功能分支/工作树。1.5.4/1.5.5 为隔离索引实验，不作默认入口。
2. **文档入口**：先读 [`docs/README.md`](docs/README.md)。子系统文档与代码路径一律指向 `packages/video-v1.5.3/`。`packages/video-v1.5.2/` 及更早是历史归档，禁止修改。
3. **凭据**：只能来自环境变量 `JY_SKILL_ROOT` `JY_DRAFT_ROOT` `JY_INSTALL_ROOT` `JY_NAS_SHARE` `JY_NAS_USER` `NAS_PASSWORD` `LARK_BASE_TOKEN` 等（完整列表见包内 `ENVIRONMENT.md`）。不得写进代码、测试、日志或提交。
4. **闸门**：`RULES.md` 的成片证据要求继续生效；formal 模式只认 `PASS`。素材缺失、窗口越界、字幕内容不符或独立 QC 失败必须拦住。哈希、签名或逐文件发布清单不是成片质量证据，禁止为普通修改新增这类门槛。
5. **提交习惯**：小步 commit；改完更新 `docs/working.md`；可复用教训写入 `LESSONS.md`。不 push `main`，只开 PR。
6. **分支命名**：功能或功能重构用 `feat/<topic>`，缺陷修复用 `fix/<topic>`，纯文档用 `docs/<topic>`；topic 使用小写英文和连字符，不以 Agent 名命名。

## 1. 按任务找文档

整个项目是纯 Windows 定时脚本流水线，入口见 [`docs/windows-pipeline.md`](docs/windows-pipeline.md)。运行不依赖编码 Agent、人工 prepare/collect 或 Mac 交接。模块化维护先读 [`docs/modular-development.md`](docs/modular-development.md)。下表中的 `orchestrator/` 均相对 `packages/video-v1.5.3/doubao-jianying-orchestrator/`，指向实际实现。旧平铺 Python 路径仅为兼容入口，禁止在其中新增业务逻辑。迁移映射见 `orchestrator/module_map.json`；公共证据合同与规则包保留原位置。闭环唯一实现为 `tools/workflow/closed_loop.py`，worker 纯合同为 `tools/queueing/contracts.py`。

| 你要改的东西 | 先读 | 代码位置 |
|---|---|---|
| 新用户入口 / 模型和环境配置 | `docs/quickstart.md`, `docs/model-and-vision.md`, `ENVIRONMENT.md`（活包内） | 活包 `examples/`、`orchestrator/runtime/vision_config.py`、`orchestrator/vision/vision_analyzer.py` |
| GitHub 协作 / 离线 CI | `CONTRIBUTING.md`, `docs/test.md` | `.github/workflows/ci.yml`, `.github/PULL_REQUEST_TEMPLATE.md` |
| 编排主流程 / 闸门顺序 / 草稿写入 | `docs/subsystems/orchestrator-engine.md` | `orchestrator/application/engine.py`、`orchestrator/domain/clips.py` |
| CLI 子命令（env/build/recover/…） | `docs/subsystems/orchestrator-cli.md` | `orchestrator/application/cli.py` |
| 选镜、视觉证据、语义闸门 | `docs/subsystems/visual-matching.md` | `orchestrator/{selection,vision,quality}/`、`policy/semantic_gate.py`；原位公共合同 `ai_editor_contract.py`、`visual_qc_contract.py` |
| 品类规则包 / 场景规则 / 相位 | `packages/video-v1.5.3/doubao-jianying-orchestrator/references/production_quality_and_lessons.md` | `orchestrator/rulepack.py`、`policy/scene_rules.py`、`vision/phase_timing.py`、`rules/*.json` |
| 口播/TTS/音色/BGM/响度 | `docs/subsystems/audio-voice.md`, `docs/programmatic-production.md` | `orchestrator/audio/`（含`final_bgm.py`业务音乐准备）、`orchestrator/policy/script_polish.py` |
| 时序契约 / 音画对齐 | `docs/subsystems/timing.md` | `orchestrator/selection/timing_contract.py`、`orchestrator/media/stability.py` |
| 编解码 / ffmpeg / GPU | `docs/subsystems/media-codec.md` | `orchestrator/media/`、`orchestrator/runtime/platform_env.py` |
| 容量 / 资源 / 任务日志 / 恢复 | `docs/subsystems/runtime-and-recovery.md` | `orchestrator/runtime/`、`quality/{diagnostics,draft_safety}.py`、`delivery/migration.py` |
| 纸品动作 / 帧域求解 / 诊断预览 | `docs/paper-action-windows.md`, `docs/paper-selection-replay.md` | `orchestrator/selection/paper_*.py`、`media/action_frames.py`；活包 `tools/{paper_selection_replay,selection_preview}.py`；尚未接入默认生产 |
| 独立时间线 / 无草稿工作预览 / 无头待审交接 | `docs/programmatic-production.md`, `docs/plans/20261101-programmatic-production.md` | `orchestrator/domain/timeline.py`、`application/production.py`、`audio/final_assets.py`、`media/timeline_render.py`、`quality/{production_qc,timeline_export}.py`、`delivery/jianying_export.py`；活包 `tools/{production,headless_review_delivery}.py`；固定选镜与待审交接显式入口，正式内容与导出验收仍待实施 |
| 剪映草稿底层操作 | `docs/subsystems/jianying-editor.md` | `packages/video-v1.5.3/jianying-editor/scripts/**`（`vendor/pyJianYingDraft` 原则上不改） |
| 定时脚本 / 队列 / 闭环 / 评估 | `docs/windows-pipeline.md`, `docs/subsystems/package-tools.md` | `packages/video-v1.5.3/batch_worker.py`, `tools/batch_dispatch.py`, `tools/workflow/closed_loop.py`, `tools/*.py` |
| 草稿交付 / 生成版恢复 | `.../references/draft_delivery_and_recovery.md` | `packages/video-v1.5.3/tools/draft_delivery.py`, `ship_distribute.py` |
| 视觉业务规则 | `packages/video-v1.5.3/RULES.md`（规则版本 1.4.1） | `WINDOWS_VIDEO_CLOSED_LOOP_RULES_*.json` |
| 环境依赖 | `packages/video-v1.5.3/ENVIRONMENT.md` | — |
| 运行时合同 / 已知坑 | `.../SKILL.md` 与 `references/*.md` | — |
| NAS 往返 | `packages/video-v1.5.3/nas-tools/使用说明.md` | `packages/video-v1.5.3/tools/nas_portable.py` |
| 发版 | `docs/release-process.md`, `docs/RELEASES.md`, `.../references/validated_fix_to_release.md` | `packages/video-v1.5.3/tools/release_package.py`, `release-manifest.json`, `VERSION`, `releases/` |
| 代码风格 | `docs/code-standards.md` | — |

## 2. 改完必须做（本地验证）

```powershell
cd packages/video-v1.5.3
uv run --frozen python -X utf8 -m unittest discover -s tests -q
uv run --frozen python -X utf8 -m unittest discover -s doubao-jianying-orchestrator/tests -q
uv run --frozen python -X utf8 -m tools.check_architecture
```

- 完整策略见 `docs/test.md`。局部交付迭代可只跑 `test_draft_delivery.py`、`test_nas_portable.py`；发版 PR 用 `tools.release_package build --output <包外新目录> --checks full`。
- 改了闸门/状态：补一个「该拦住的输入确实被拦住」的测试。
- 测试通过 ≠ 生产通过。不要在 PR/回复里声称「已上线/已验收」。

常用 CLI：

```powershell
cd packages/video-v1.5.3/doubao-jianying-orchestrator
uv run --frozen python -X utf8 -m orchestrator.cli env
uv run --frozen python -X utf8 -m orchestrator.cli build --input /abs/manifest.json --report-dir .jy_run
uv run --frozen python -X utf8 -m orchestrator.cli recover --input /abs/manifest.json --stage draft_write --report-dir .jy_run
uv run --frozen python -X utf8 -m orchestrator.cli review-script --input /abs/manifest.json --report-dir .jy_run
```

## 3. 不许做

- 不将默认 GLM-5.3-Flash（`glm-5.3-flash`）简称为 GLM-5.3 或 ChatGLM3；transport、模型 ID、选镜模式与 QC 版本分开说明，改默认值时同步入口文档。
- 运行时视觉与文字请求只走 GLM（`volc_glm` / `glm` / `volc`）或 Qwen（`qwen` / `qwen_compatible`）直连。不得恢复 Codex CLI、Anthropic 或 CCSwitch 调用；旧通道和未知值必须明确拒绝。`legacy` / `rule_then_ai` 是保留的选镜策略，不是旧调用后端。
- 历史 JyPoll/JyRun 执行链已退役；`jy_poll.ps1` 与 `run_task.template.ps1` 仅保留立即失败的入口，不恢复 Agent 调度。整体运行从 `scripts/run_pipeline.ps1` 或 `python -m tools.workflow.scheduled` 进入；内部复用 dispatcher。注册计划任务须显式运行安装脚本，开发验证不启用真实队列或改宿主计划任务。
- 不 push 到 main；只开 PR。不改历史版本目录。不改 `vendor/` 除非 PR 说明原因。
- 不放宽 `DIRECT → ACCEPTABLE_DEGRADED → MATERIAL_GAP` 顺序，不让 preview 稿进入正式交付。
- 不在提示词或代码里手写版本号；版本从 `VERSION` 文件回读。
- 不把 `docs/working.md` / `LESSONS.md` 当成可选项；有行为变更就要留痕迹。

## 4. 学到新东西

写进 `LESSONS.md`（格式见文件头），并在 `docs/working.md` 当天 Changelog 加一条。同一教训出现 3 次 → 提 PR 升级进本文件第 0/3 节或 `docs/code-standards.md`，并在 `LESSONS` 中标记 `PROMOTED`。
