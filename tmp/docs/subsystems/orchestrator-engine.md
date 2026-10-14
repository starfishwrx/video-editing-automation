# 子系统：编排引擎（orchestrator-engine）

- 代码：`packages/video-v1.5.1/doubao-jianying-orchestrator/orchestrator/engine.py`（约 2750 行，核心类 `OrchestrationEngine`、数据类 `Clip`）
- 入口：`cli.py` 的 `build` 子命令 → `OrchestrationEngine(manifest, base_dir, report_dir, ...).run()`
- 相关规则：`RULES.md`（视觉规则 1.4.1）、`SKILL.md`、`references/runtime_contract.md`

## 1. 职责
把一份任务 manifest 变成一个可编辑的剪映草稿：素材探测 → 口播/选声/视觉证据闸门 → 稳定切片 → TTS → 音画对齐 → 不变量校验 → 转场 → BGM → 安全写草稿。所有时间内部统一为**整数微秒**（`US = 1_000_000`，`to_us()`）。
不负责：视觉分析本身（shot_analyzer/vision_analyzer，在引擎构造前由 CLI 跑完）、队列调度（batch_worker）、表格/NAS 回写（tools/）。

## 2. `run()` 的闸门顺序（任何一步返回 dict 即提前终止）
1. `self_check()` 环境自检（版本下限 `MIN_SUPPORTED_PACKAGE_VERSION="v1.3.13"`，是**下限不是等值**）
2. `mode_confirmation_gate()` 智能模式只能由用户显式触发（`trigger_phrase` ∈ 智能编排/智能生成/开启智能模式 或 `mode_user_confirmed`）
3. `build_clips()` → `_script_segment_length_gate()`；`visual_missing` 段记入 `preview_uncertified_reasons`
4. 归一化 `delivery_mode` 写回 manifest（formal 默认；preview/preview_only/预览 → preview）
5. `voiceover_gate()` → `audio_selection_gate()`
6. `decide_voice()`；音色不明确返回 `VOICE_SELECTION_REQUIRED`，**不回退到其他音色**
7. `visual_evidence_gate()` → `_visual_duplicate_gate()`
8. `_upload_videos_and_check_capacity()` → `_background_pressure("before_processing")`
9. `script_review` → `stabilize` → `score_storyboard` → 压力检查 → `synthesize`（TTS）→ 压力检查
10. `align_audio_video()` → `validate_audio_video_sync()` → `_invariant_gate(stage="plan_complete")` → `final_visual_evidence_gate()`
11. `plan_transitions()` → `prepare_bgm()`（失败返回 `BGM_REQUIRED`）→ `write_draft()`

## 3. 关键不变量
- formal：所有门禁严格；preview：时序未解片段、视觉证据缺口可出稿但必须写入 `preview_uncertified_reasons`，结果 `formal_delivery_allowed=False`。
- 单分镜字幕 ≤ 15 字（`MAX_SUBTITLE_CHARS`）；字幕规格见 RULES.md。
- 兜底段在画面上方打【素材待补】（`Degraded_Marks` 轨）；未认证批次打【本批未认证】（`Uncertified_Marks` 轨）。
- 探测缓存在 `report_dir/.cache/media_probe.json`，口径变更时递增 `PROBE_KIND`。
- 影响选镜的 manifest 字段必须在 CLI 侧写入（见 LESSONS L-SEED-03）。

## 4. 输入 / 输出
- 输入：manifest（`segments[]` 每项含 video/source_start/duration/text/claim_text/audio_mode/voice/evidence_*、timing 合同字段 source_end/audio_duration_us/timing_certainty/video_speed 等；完整字段表 **待补：从 build_clips 与 runtime_contract.md 汇总**）
- 输出：status dict（已见状态：`ERROR`、`VOICE_SELECTION_REQUIRED`、`BGM_REQUIRED` 及各 gate 返回值；完整枚举 **待补**），草稿目录，report_dir 下证据文件。

## 5. 依赖模块
audio_loudness, bgm_selector, capacity, draft_safety, script_polish, semantic_gate, stability, timing_contract, voice_tts, visual_match, platform_env, resource_profile, video_encode, voice_catalog；草稿写入依赖 jianying-editor。

## 6. 测试
`doubao-jianying-orchestrator/tests/`：test_capacity_and_sync, test_task_control, test_timing_contract, test_video_decode, test_video_encode, test_visual_matching_governance。
**缺口**：没有专门针对 `run()` 闸门顺序的端到端测试（待确认）；建议加"每道闸门各给一个必拦输入"的参数化测试。

## 7. 改动检查单
- [ ] 不改变闸门相对顺序，或在 PR 中说明理由
- [ ] 新状态码同步到本文档第 4 节与 runtime_contract.md
- [ ] 不让 preview 路径产出 `PASS`
- [ ] 两套测试全绿

## 8. 已知坑
见 LESSONS.md 中模块=orchestrator-engine 的条目与 `references/preview_gap_and_pitfalls.md`。
