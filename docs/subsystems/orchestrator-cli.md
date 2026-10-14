# 子系统：命令行（orchestrator-cli）

- 代码：`packages/video-v1.5.1/doubao-jianying-orchestrator/orchestrator/cli.py`
- 运行：进入 `doubao-jianying-orchestrator` 后 `python -m orchestrator.cli <subcommand>`
- 说明：stdout 仅输出机器可读 JSON；详细运行日志写入 `report/tasks/<run_id>/events.jsonl`

## 1. 子命令与退出码
- `env`：环境自检（FFmpeg、剪映根、包签名、依赖、草稿根可写性等）。0=ready，非 0=未就绪。
- `setup-ffmpeg [--force]`：Windows 绿色 FFmpeg 自动引导；成功返回 `READY/ALREADY_READY`。
- `list-voices`：列出可选音色（供运营二选一）。
- `recommend-voice [--text --style --persona --topk]`：按脚本/人设推荐音色。
- `audition [--script --topn --gender --keys --all --out-dir]`：按剧情生成试听 MP3（固定样词）；返回可播放文件列表。
- `list-library [--gender --kw]`：浏览剪映全量音色库。
- `recommend-bgm [--script --mood --id --min-dur --topk]`：BGM 推荐与试听。
- `scan --input --report-dir`：仅扫描素材元数据并落盘 `asset_metadata.json`。
- `analyze-shots --input --report-dir [--threshold --min-shot-s --no-vision]`：仅做本批素材抽帧/描述/标签 → `shot_candidates.json`。
- `match-shots --input --analysis --report-dir [--topk]`：按脚本/卖点对 `shot_candidates.json` 做逐句 Top3，写 `shot_match_report.json`。
- `review-script --input --report-dir`：口播确认单（逐句字数/预估时长/衔接覆盖体检），并对有口播的分镜做一次“源证据闸门”体检。
- `build --input --report-dir [--strict-script] [--delivery-mode formal|preview]`：生成草稿。0=SUCCESS，非 0=其他状态（JSON 内含 `terminal_state/next_action`）。
- `diagnose-timeline --draft --report-dir [--expect-external-package]`：只读诊断草稿结构/媒体/导入状态（不驱动 UI）。
- `recover --input --report-dir [--stage preflight|semantic_match|draft_write|ui_acceptance] [--draft]`：分阶段恢复；不覆盖原稿。
- `migrate-encrypted --source-draft --new-name [--wait-seconds]`：调用剪映官方“副本迁移”并在本地验证（仅 Windows UI 环境可做真实迁移）。
- `calibrate-capacity --input --test-root --report-dir [--step --max-sources --profile-path]`：隔离增量压测，标定“每条成片安全素材上限”（持久化 profile）。

注意：未提供子命令时，兼容旧用法 `--input` 语义；“智能编排/开启智能模式”短语会路由到 `build --trigger-phrase`。

## 2. 输出合同（精简版）
- `build` 的 stdout 是 `_compact_build_result(result)`：在 `SUCCESS` 时仅列出草稿名称/路径/关键文件与 `task_log`；在阻断/未认证/待确认时，保留 `status`、`failure_class`、`retryable`、`next_action`、`report` 等关键字段，便于对话面板渲染。
- `recover` 的 stdout 是 `_compact_control_result(result)`：保留 `status/next_action/recovery_stage`、`matched_manifest(_sha256)`、`task_log`。

## 3. 关键约束与口径
- `delivery_mode` 必须在选镜之前就写入 manifest（`cmd_build` 会归一化并注入）—— `match_manifest` 要用它决定预览黑屏/补镜与“未解出窗口”的处置；引擎仅读取并在结果里报告。
- 音色仅在 `cmd_build` 里唯一决议并回写 manifest（`voice_catalog.resolve_voice_once`）；引擎后续只读锁定结果。
- 选镜前的 `pre_synthesize_narration` 只用于拿逐句真实音频时长真值（命中同一缓存，不额外花钱），并据此跑候选窗口求解。
- 任何“输入素材不存在/环境未就绪”都作为 `ENV_ERROR` 在真正开销前停止，不得被包装成可重试的“模型失败”。

## 4. 依赖与缓存
- 通过 `platform_env.environment_report()` 解析剪映根、草稿目录、FFmpeg/ffprobe、包签名、依赖状态。
- 视觉理解缓存：`vision_analyzer` 会将“帧描述/标签/动作确认”的稳定投影写入跨任务 `.cache/material_vision.json`（key 受素材/窗口/profile 约束）。
- 旁白/TTS 批量合成缓存：`voice_tts` 的缓存键仅包含“文本+音色+后端+后处理版本”，与视频速度/窗口无关，命中即复用字节。

## 5. 测试与示例
- 参见 `packages/video-v1.5.1/doubao-jianying-orchestrator/tests/test_task_control.py` 等，覆盖 `result_contract`、阶段路由、恢复阶段与错误路径。

