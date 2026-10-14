# 其余子系统文档大纲（每篇都沿用 orchestrator-engine.md 的 8 节结构：职责/流程/不变量/输入输出/依赖/测试/检查单/已知坑）

## orchestrator-cli.md — cli.py, run.py
子命令表（env, setup-ffmpeg, list-voices, recommend-voice, audition, list-library, recommend-bgm, scan, analyze-shots, match-shots, review-script, build, diagnose-timeline, recover, migrate-encrypted, calibrate-capacity）；退出码；`_compact_build_result` 输出合同；recover 不覆盖原稿；TaskLogger 单任务锁。

## visual-matching.md — visual_match, shot_analyzer, vision_analyzer, semantic_gate, draft_visual_qc, feedback_policy, prompts/semantic_analysis.md
候选池（上限 24）；DIRECT→ACCEPTABLE_DEGRADED→MATERIAL_GAP；PACKAGING_CLAIM_DIRECT；CTA 证据；preview 黑屏补镜与 REUSE；视觉服务一次有限重试；volc profile。

## audio-voice.md — voice_tts, voice_catalog, voice_library, bgm_selector, audio_loudness, script_polish
音色解析与歧义处理；TTS 并发；响度标准；BGM 来源与失败语义。

## timing.md — timing_contract, stability
WindowRequest；frame_speed；微秒口径；尾部留白 TAIL_PAD；证据窗口覆盖。

## media-codec.md — video_encode, video_decode, ffmpeg_bootstrap, platform_env
GPU 优先；4K HEVC CUDA `-threads 1`；ffmpeg 查找顺序；跨平台差异（Windows 渲染机 / Mac 审阅）。

## runtime-and-recovery.md — capacity, resource_profile, task_log, diagnostics, migration, draft_safety, stability
result.json 合同（terminal_state/failure_class/retryable/safe_to_continue/next_action/parent_run_id）；分阶段恢复；加密草稿迁移；容量标定。

## jianying-editor.md — jianying-editor/scripts/{core,utils}, jy_wrapper.py, draft_inspector.py, cloud_manager.py, universal_tts.py
与 vendor/pyJianYingDraft 的边界（第三方，版本/来源待补）；明文 draft_info.json 出货规则。

## package-tools.md — batch_worker.py, closed_loop.py（根目录与 tools/ 各一份，需确认关系）, tools/*.py, jy_poll.ps1, run_task.template.ps1, gold-reference.json
队列守卫、done.json 合同、JY_TASK_SKIP_CURRENT、失败回执、eval_bootstrap/eval_stage/production_eval、mac_review_handoff、material_index/library、delivery_identity、classify_feedback。

## 另需新增
- docs/code-standards.md：Python 版本（待确认）、整数微秒、状态枚举、禁止硬编码路径/凭据、中文注释写"为什么"、新闸门必须带拦截测试。
- docs/release-process.md：从 RELEASES.md 与 README "发布验收"整理成可执行步骤。
- docs/architecture.md：一张端到端数据流图（manifest → analyze → match → build → QC → 人工 → 回写）。
