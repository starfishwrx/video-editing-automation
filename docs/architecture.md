# 体系结构（活包 video-v1.5.3 / VERSION 见包内）

端到端数据流（粗体为阶段产物）：

1) 输入准备
- 外部注入运行环境（凭据/路径/FFmpeg/剪映根）→ `platform_env.environment_report()` 体检
- 清单 `manifest.json`（素材/脚本/参数）

2) 视觉候选生成与匹配
- `shot_analyzer.analyze_manifest`：逐源抽帧、场景切分、代表帧 → 写 `report/shot_candidates.json`、抽帧目录
- `vision_analyzer.enrich_with_vision`：按 contact sheet 批量识别描述/标签/动作 → 回写 `shots[*]` 并缓存可复用理解
- `vision_analyzer.match_with_codex`（可选）：在“已观察描述”上做句-镜匹配补充候选
- `visual_match.match_manifest`：逐句 Top3、自动采用 Top1；直证优先、语义达标次之；可行窗口求解 + 复用/相邻/机位闸门；必要时多镜组合；preview 黑屏/补镜
  → 产出“可写稿”的 `matched_manifest`（含 `segments[*]` 的窗口/证据/速度/多镜/预览未解等）

3) 编排执行（引擎）
- `orchestrator.engine.OrchestrationEngine.run()`：顺序闸门
  - 环境自检 → 模式确认（formal/preview 归一化）→ 口播/TTS 锁定与选择 → 视觉证据闸门（两次：初判与最终覆盖）→
    稳定切片与窗口装配 → 音画对齐与时序不变量校验 → 转场/BGM → 安全写稿（`jianying-editor`）
- 产物：草稿目录、`report/` 证据、`last_result.json`（终态 + `task_log.result_contract` 附加合同）

4) 独立后验 QC（成片视觉与结构）
- `draft_visual_qc.py`：生成独立抽帧并调用视觉审读 → `draft_visual_qc.json`（每句 `DIRECT/DEGRADED/MISMATCH/INSUFFICIENT` + overall ）
- `diagnostics.diagnose_timeline`：只读解析时间线结构/媒体图/导入状态

5) 闭环与交付（Windows 工具链）
- 队列与任务闭环：`batch_worker.py`（`done.json` 合同、`JY_TASK_SKIP_CURRENT`、快照）
- 成功交付：`closed_loop.py` 校验 `QC/PASS`、Mac 审阅绑定、打包/分组、表格回写（受 `WINDOWS_VIDEO_CLOSED_LOOP_RULES_*` 约束）
- 失败回执：`closed_loop.py failure-update` 与 `tools/classify_feedback.py` 分类整理

6) 运行时契约与时序一致性
- “唯一真时钟”：`timing_contract`（µs 口径、证据覆盖/速度范围/把手/防线校验），规划/引擎共用
- 旁白响度与 BGM 铺底：`audio_loudness` 与 `bgm_selector`（LUFS 口径与闭环复测）
- 媒体解/编码：`video_decode`（CUDA/回退）、`video_encode`（NVENC 优先/级联回退）

关键不变量与验收要点：
- 口播段：`segment_us == audio_duration_us + tail_pad_us`（容差 ≤1 帧）；
- 源窗口几何链：`0 <= source_start < source_end <= material_duration_us`（零容差）；
- 证据覆盖：最终窗口覆盖 `evidence_[start,end]_us`（或帧邻域）；preview 仅记未认证；
- 速度范围：随 `visual_role` 取值；与时间相关的直接证据锁 1.0，不允许靠变速伪造；
- 预览：可出图但不得用于正式交付；成品与结果带“未认证”与逐条理由。

