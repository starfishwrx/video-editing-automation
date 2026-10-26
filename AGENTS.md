# AGENTS.md — 路由表（给 Agent 看的第一份文件）

> 本文件只做指路。产品目标见 `docs/prd.md`，架构定案见 `docs/rfc.md`，按日记录见 `docs/working.md`，验证完成标准见 `docs/test.md`。新坑写进 `LESSONS.md`，并在当天 `docs/working.md` Changelog 留一条。

## 0. 先读这五条

1. **版本身份**：可改的候选包目录是 `packages/video-v1.5.3/`（目录名是历史代号，不随小版本改名）。真实版本只读该目录下的 `VERSION`（当前主线 **1.5.6**）。生产以部署机器的 `ACTIVE_VIDEO_RUNTIME.json` 与已安装技能哈希为准。开发使用从 `main` 创建的干净功能分支/工作树。1.5.4/1.5.5 为隔离索引实验，不作默认入口。
2. **文档入口**：先读 [`docs/README.md`](docs/README.md)。子系统文档与代码路径一律指向 `packages/video-v1.5.3/`。`packages/video-v1.5.2/` 及更早是历史归档，禁止修改。
3. **凭据**：只能来自环境变量 `JY_SKILL_ROOT` `JY_DRAFT_ROOT` `JY_INSTALL_ROOT` `JY_NAS_SHARE` `JY_NAS_USER` `NAS_PASSWORD` `JY_MAC_DRAFT_ROOT` `LARK_BASE_TOKEN` 等（完整列表见包内 `ENVIRONMENT.md`）。不得写进代码、测试、日志或提交。
4. **闸门**：任何改动都不能让 `RULES.md` 的证据闸门变松；formal 模式只认 `PASS`；模型超时/证据缺失/版本漂移只能是 `UNCERTIFIED` 或 `FAIL`。
5. **提交习惯**：小步 commit；改完更新 `docs/working.md`；可复用教训写入 `LESSONS.md`。不 push `main`，只开 PR。

## 1. 按任务找文档

| 你要改的东西 | 先读 | 代码位置 |
|---|---|---|
| 新用户入口 / 模型和环境配置 | `docs/quickstart.md`, `docs/model-and-vision.md`, `ENVIRONMENT.md`（活包内） | `examples/`（活包内）、`vision_analyzer.py` |
| GitHub 协作 / 离线 CI | `CONTRIBUTING.md`, `docs/test.md` | `.github/workflows/ci.yml`, `.github/PULL_REQUEST_TEMPLATE.md` |
| 编排主流程 / 闸门顺序 / 草稿写入 | `docs/subsystems/orchestrator-engine.md` | `packages/video-v1.5.3/doubao-jianying-orchestrator/orchestrator/engine.py` |
| CLI 子命令（env/build/recover/…） | `docs/subsystems/orchestrator-cli.md` | `packages/video-v1.5.3/doubao-jianying-orchestrator/orchestrator/cli.py` |
| 选镜、视觉证据、语义闸门 | `docs/subsystems/visual-matching.md` | `.../orchestrator/ai_editor.py`, `ai_editor_contract.py`, `ai_editor_qc.py`, `shot_analyzer.py`, `vision_analyzer.py`, `semantic_gate.py`, `draft_visual_qc.py`；旧 matcher 仅 legacy |
| 品类规则包 / 场景规则 / 相位 | `packages/video-v1.5.3/doubao-jianying-orchestrator/references/production_quality_and_lessons.md` | `.../orchestrator/rulepack.py`, `scene_rules.py`, `phase_timing.py`, `rules/*.json` |
| 口播/TTS/音色/BGM/响度 | `docs/subsystems/audio-voice.md` | `.../orchestrator/voice_tts.py`, `voice_catalog.py`, `voice_library.py`, `bgm_selector.py`, `audio_loudness.py`, `script_polish.py` |
| 时序契约 / 音画对齐 | `docs/subsystems/timing.md` | `.../orchestrator/timing_contract.py`, `stability.py` |
| 编解码 / ffmpeg / GPU | `docs/subsystems/media-codec.md` | `.../orchestrator/video_encode.py`, `video_decode.py`, `ffmpeg_bootstrap.py`, `platform_env.py`, `gpu_runtime.py` |
| 容量 / 资源 / 任务日志 / 恢复 | `docs/subsystems/runtime-and-recovery.md` | `.../orchestrator/capacity.py`, `resource_profile.py`, `task_log.py`, `diagnostics.py`, `migration.py`, `draft_safety.py` |
| 剪映草稿底层操作 | `docs/subsystems/jianying-editor.md` | `packages/video-v1.5.3/jianying-editor/scripts/**`（`vendor/pyJianYingDraft` 原则上不改） |
| 队列 / 闭环 / 评估 | `docs/subsystems/package-tools.md` | `packages/video-v1.5.3/batch_worker.py`, `closed_loop.py`, `tools/*.py`, `run_task.template.ps1` |
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
- 不 push 到 main；只开 PR。不改历史版本目录。不改 `vendor/` 除非 PR 说明原因。
- 不放宽 `DIRECT → ACCEPTABLE_DEGRADED → MATERIAL_GAP` 顺序，不让 preview 稿进入正式交付。
- 不在提示词或代码里手写版本号；版本从 `VERSION` 文件回读。
- 不把 `docs/working.md` / `LESSONS.md` 当成可选项；有行为变更就要留痕迹。

## 4. 学到新东西

写进 `LESSONS.md`（格式见文件头），并在 `docs/working.md` 当天 Changelog 加一条。同一教训出现 3 次 → 提 PR 升级进本文件第 0/3 节或 `docs/code-standards.md`，并在 `LESSONS` 中标记 `PROMOTED`。
