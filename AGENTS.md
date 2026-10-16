# AGENTS.md — 路由表（给 Agent 看的第一份文件）

> 本文件只做“指路”，不展开细节。实现与边界条件放在 docs/ 下；每次改完代码，如有新坑，写进 `LESSONS.md`。

## 0. 先读这三条
1. 当前可改的候选包是 `packages/video-v1.5.3/`。下表中 v1.5.1 的子系统文档仍可用于定位，但实际代码读取 v1.5.3 的同名路径。`packages/video-v1.5.1/`、`packages/video-v1.5.0/`、`packages/video-v1.4.0/` 是历史归档，禁止修改；它们与校验清单绑定。
2. 凭据/机器路径只能来自环境变量：`JY_SKILL_ROOT` `JY_DRAFT_ROOT` `JY_INSTALL_ROOT` `JY_NAS_SHARE` `JY_NAS_USER` `NAS_PASSWORD` `JY_MAC_DRAFT_ROOT` `LARK_BASE_TOKEN`。不得写进代码、测试、日志或提交。
3. 任何改动都不能让 `RULES.md` 的证据闸门变松：formal 模式只认 `PASS`；模型超时/证据缺失/版本漂移只能是 `UNCERTIFIED` 或 `FAIL`。

## 1. 按任务找文档（历史路由对应当前包的同名模块）
| 你要改的东西 | 先读 | 代码位置 |
|---|---|---|
| 编排主流程 / 闸门顺序 / 草稿写入 | `docs/subsystems/orchestrator-engine.md` | `packages/video-v1.5.1/doubao-jianying-orchestrator/orchestrator/engine.py` |
| CLI 子命令（env/build/recover/…） | `docs/subsystems/orchestrator-cli.md` | `packages/video-v1.5.1/doubao-jianying-orchestrator/orchestrator/cli.py` |
| 选镜、视觉证据、语义闸门 | `docs/subsystems/visual-matching.md` | `.../orchestrator/visual_match.py`, `shot_analyzer.py`, `vision_analyzer.py`, `semantic_gate.py`, `draft_visual_qc.py`, `feedback_policy.py` |
| 口播/TTS/音色/BGM/响度 | `docs/subsystems/audio-voice.md` | `.../orchestrator/voice_tts.py`, `voice_catalog.py`, `voice_library.py`, `bgm_selector.py`, `audio_loudness.py`, `script_polish.py` |
| 时序契约 / 音画对齐 | `docs/subsystems/timing.md` | `.../orchestrator/timing_contract.py`, `stability.py` |
| 编解码 / ffmpeg / GPU | `docs/subsystems/media-codec.md` | `.../orchestrator/video_encode.py`, `video_decode.py`, `ffmpeg_bootstrap.py`, `platform_env.py` |
| 容量 / 资源 / 任务日志 / 恢复 | `docs/subsystems/runtime-and-recovery.md` | `.../orchestrator/capacity.py`, `resource_profile.py`, `task_log.py`, `diagnostics.py`, `migration.py`, `draft_safety.py` |
| 剪映草稿底层操作 | `docs/subsystems/jianying-editor.md` | `packages/video-v1.5.1/jianying-editor/scripts/**`（`vendor/pyJianYingDraft` 为第三方，原则上不改） |
| 队列 / 闭环 / 评估 / 交付 | `docs/subsystems/package-tools.md` | `packages/video-v1.5.1/batch_worker.py`, `packages/video-v1.5.1/closed_loop.py`, `packages/video-v1.5.1/tools/*.py`, `run_task.template.ps1`, `jy_poll.ps1` |
| 视觉业务规则 | `packages/video-v1.5.1/RULES.md`（规则版本 1.4.1） | `WINDOWS_VIDEO_CLOSED_LOOP_RULES_*.json` |
| 环境依赖 | `packages/video-v1.5.1/ENVIRONMENT.md` | — |
| 运行时合同 / 已知坑 | `packages/video-v1.5.1/doubao-jianying-orchestrator/SKILL.md` 与 `references/*.md` | — |
| 发版 | `docs/release-process.md`, `docs/RELEASES.md` | `packages/video-v1.5.1/release-manifest.json`, `VERSION`, `releases/` |
| 已验证修复合包 / 素材丢失修复 | `packages/video-v1.5.3/doubao-jianying-orchestrator/references/validated_fix_to_release.md` | `packages/video-v1.5.3/tools/release_package.py`, `draft_delivery.py` |
| 代码风格 | `docs/code-standards.md` | — |

## 2. 改完必须做（本地验证）
```powershell
cd packages/video-v1.5.3
uv run --frozen python -X utf8 -m unittest discover -s tests -q
uv run --frozen python -X utf8 -m unittest discover -s doubao-jianying-orchestrator/tests -q
```
- 局部交付迭代可只运行 `test_draft_delivery.py`、`test_nas_portable.py`；准备发版 PR 时，用 `tools.release_package build --output <包外新目录> --checks full` 在解压副本跑两套测试一次。
- 改了闸门/状态：补一个“该拦住的输入确实被拦住”的测试。
- 改了文件：按 `docs/release-process.md` 重新生成 `PACKAGE_CONTENTS.sha256`（只在发版 PR 中做）。
- 测试通过 ≠ 生产通过。不要在 PR/回复里声称“已上线/已验收”。

常用 CLI（以 v1.5.1 为例）：
```bash
# 进入 orchestrator 包根后调用：orchestrator 作为包名
cd packages/video-v1.5.1/doubao-jianying-orchestrator
python -m orchestrator.cli env
python -m orchestrator.cli build --input /abs/manifest.json --report-dir .jy_run
python -m orchestrator.cli recover --input /abs/manifest.json --stage draft_write --report-dir .jy_run
python -m orchestrator.cli review-script --input /abs/manifest.json --report-dir .jy_run
```

## 3. 不许做
- 不 push 到 main；只开 PR。不改历史版本目录。不改 `vendor/` 除非 PR 说明原因。
- 不放宽 `DIRECT → ACCEPTABLE_DEGRADED → MATERIAL_GAP` 顺序，不让 preview 稿进入正式交付。
- 不在提示词或代码里手写版本号；版本从 `VERSION` 文件回读。

## 4. 学到新东西
写进 `LESSONS.md`（格式见文件头）。同一教训出现 3 次 → 提 PR 把它升级进本文件第 0/3 节或 `docs/code-standards.md`，并在 `LESSONS` 中标记 `PROMOTED`。

