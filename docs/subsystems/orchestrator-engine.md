# 子系统：编排引擎（orchestrator-engine）

- 代码：`packages/video-v1.5.3/doubao-jianying-orchestrator/orchestrator/engine.py`
- 入口：`cli.build`/`cli.recover` → `OrchestrationEngine(manifest, base_dir, report_dir, ...).run()`
- 相关：`RULES.md`（1.4.1）、`SKILL.md`、`references/*`、`timing_contract.py`

## 1. 职责
把一份任务 manifest 变成可编辑的剪映草稿：素材探测 → 口播/音色选择 → 视觉证据闸门 → 稳定切片与窗口装配 → TTS/音画对齐 → 不变量校验 → 转场/BGM → 安全写草稿。引擎内部统一“整数微秒”（`US=1_000_000`，详见 `timing_contract`）。
不负责：视觉分析本身（`shot_analyzer/vision_analyzer` 在引擎构造前由 CLI 跑完）、队列/表格/分发（`batch_worker` 与 tools）。

## 2. 闸门与阶段顺序（任意一步返回 dict 即提前终止）
1. `self_check()`：环境自检（最低支持 `MIN_SUPPORTED_PACKAGE_VERSION="v1.3.13"`，是“下限”，不是等值）
2. `_resolve_delivery_mode()`：formal（默认）/preview 归一化并写回对象字段（preview 只影响“容忍未认证”，不放宽 formal 语义）
3. `build_clips()` → `_script_segment_length_gate()`（超长口播阻断）
4. 归一化 `delivery_mode` 写回 manifest（CLI 已在选镜前写入；引擎仅读取/归一化）
5. `voiceover_gate()`（本批是否有旁白）→ `audio_selection_gate()`（需要运营确认音色/BGM 时暂停）
6. `decide_voice()`（锁定一次音色；不静默换音色，失败返回 `VOICE_SELECTION_REQUIRED`）
7. `visual_evidence_gate()`（源证据与 AI/人工标签校验；preview 仅记 `preview_uncertified_reasons`）
8. `_upload_videos_and_check_capacity()` → `_background_pressure()`（资源压力）
9. `script_review` → `stabilize` → `score_storyboard` → `synthesize`（必要时 TTS）→ 压力检查
10. `align_audio_video()` → `validate_audio_video_sync()`（需求3/5 的等式校验与容差）→ `_invariant_gate(stage="plan_complete")` → `final_visual_evidence_gate()`
11. `plan_transitions()` → `prepare_bgm()`（失败返回 `BGM_REQUIRED`）→ `write_draft()`（安全写稿与 post-write 校验）

## 3. 关键不变量
- formal：所有门禁严格；preview：时序未解/证据缺口可出稿，但写入 `preview_uncertified_reasons`，`formal_delivery_allowed=False`。
- 口播段时间等式：`segment_us == audio_duration_us + tail_pad_us`（容差 ≤1 帧，`timing_contract.check_invariants`）
- 源窗口几何链：`0 <= source_start < source_end <= material_duration_us`（零容差；与写入层同尺）
- 最终切片必须覆盖证据区间；证据窗口来自 AI 明确时间段或“审核帧邻域”
- 角色决定速度范围：与时间相关的直接证据（`time_sensitive=True`）锁 1.0，不得靠变速伪造
- 预览稿：不得标成正式交付；结果中带 `coverage.certification != CERTIFIED` 与逐条未认证理由；成片打“未认证”标注

## 4. 输入/输出（字段与状态）
- 输入 manifest（常用键，具体以 `visual_match.match_manifest` 产物为准）：
  - 全局：`delivery_mode`（formal/preview）、`temporary_material_analysis`、`preview_unresolved[]`、`visual_missing_matches[]`、`max_source_reuse`、`avoid_adjacent_source_repeat`
  - 段落 `segments[*]`（选镜产物会补齐）
    - 基本：`video` `source_start` `source_end` `duration` `text` `claim_text` `intent/intents`
    - 证据：`visual_requirements` `evidence_tags` `visual_tags` `visual_description/description`
    - 动作语义：`action` `result` `object` `action_complete`
    - 时序契约：`audio_duration_us` `timing_certainty` `video_speed` `window_solution{}` `window_reason`
    - 场景边界：`scene_source_start` `scene_source_end`
    - 多镜：`multi_shot` `span_parts[]`（每段：素材窗口/证据/速度/解）`span_solution{}`
    - 预览占位：`visual_missing` `visual_missing_reason(_code)` `preview_unresolved` `preview_reason` `substitute_reason`
    - 统计/复用：`selection_mode` `degraded_no_match` `selection_score/selection_margin` `source_reuse_count`
    - 其他：`temporary_shot_id` `persist_to_library` `head_waste/tail_waste` `visual_role` `time_sensitive` `cta_visual_evidence{}` `claim_gate{}` `packaging_claim{}`
- 终态 result（常见状态码）：
  - 环境/前置：`ENV_ERROR` `TASK_ALREADY_RUNNING` `MODE_CONFIRMATION_REQUIRED`
  - 口播/选择：`NEED_VOICEOVER` `AUDIO_SELECTION_REQUIRED` `VOICE_SELECTION_REQUIRED` `BGM_REQUIRED` `BGM_PREVIEW_UNAVAILABLE`
  - 视觉/证据：`VISUAL_EVIDENCE_REQUIRED` `VISUAL_MISSING`
  - 时序/不变量：`TIMING_INFEASIBLE` `AUDIO_VIDEO_MISMATCH` `TIMING_INVARIANT_VIOLATION` `PREVIEW_BLOCKED_BY_INVARIANTS`
  - 写稿：`DRAFT_ROOT_MISMATCH` `DRAFT_NOT_CREATED` `DRAFT_INVALID`
  - 成功：`SUCCESS`（可能带 `delivery_mode=preview` 与 `coverage.uncertified_reasons`）
  - 这些状态经 `task_log.result_contract` 归一化为：`terminal_state`（SUCCESS/UNCERTIFIED/BLOCKED/ERROR/DEFERRED）、`failure_class`、`retryable`、`safe_to_continue`、`next_action`

## 5. 依赖模块
`audio_loudness` `bgm_selector` `capacity` `draft_safety` `script_polish` `semantic_gate` `stability` `timing_contract` `voice_tts` `visual_match` `platform_env` `resource_profile` `video_encode` `voice_catalog`；草稿写入依赖 `jianying-editor/scripts/**`。

## 6. 测试
`packages/video-v1.5.3/doubao-jianying-orchestrator/tests/`：`test_capacity_and_sync`、`test_task_control`、`test_timing_contract`、`test_video_decode`、`test_video_encode`、`test_visual_matching_governance`（时序/窗口/复用/预览语义覆盖）。

## 7. 改动检查单
- [ ] 不改变闸门相对顺序；若调整，PR 必须说明理由与回归用例
- [ ] 新状态码同步到本文“4.输入/输出”和 `task_log.result_contract`
- [ ] 预览路径不得产出 PASS；成片/结果带未认证理由
- [ ] 两套测试全绿；必要时补“必拦样例”参数化测试

## 8. 已知坑
- 版本闸门必须“下限”判断（见 `LESSONS.md#L-SEED-01`）
- 选镜前必须在 CLI 写入 `delivery_mode`（见 `LESSONS.md#L-SEED-03`）
- 4K HEVC CUDA 解码需 `-threads 1`，否则 NVDEC 静默退回 CPU（见 `LESSONS.md#L-SEED-02`）

