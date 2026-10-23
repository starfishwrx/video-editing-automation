# 子系统：时序契约（timing）

- 代码：`packages/video-v1.5.3/doubao-jianying-orchestrator/orchestrator/timing_contract.py`, `stability.py`

## 1. 职责
统一“够不够长”的唯一真时钟：以 µs 为单位，在规划与引擎两侧用同一套求解器/不变量，解决“估长/占位探针/最终裁决不一致”导致的整支拦截。

## 2. 口径与常量
- µs 口径：`US=1_000_000`；`frame_us(fps)`；`DEFAULT_FPS=30`
- 尾留白/保护区：`DEFAULT_TAIL_PAD_US=120_000`，`DEFAULT_HEAD_GUARD_US=300_000`，`DEFAULT_TAIL_GUARD_US=200_000`
- 速度范围：随 `visual_role`（`SPEED_RANGE_BY_ROLE`）；与时间相关的直接证据锁 1.0；缺失/未知时锁 1.0
- 真实音频时长：优先 ffprobe；合成失败时显式降级（`timing_certainty="estimated"`），绝不在正常路径伪装成真值

## 3. 素材时长探针（与写入层同尺）
`probe_material_duration_us(path)`：返回 `{duration_us, source, confidence}`，优先与写入层一致的视频轨时长（`HIGH`）；仅容器时长时“减一帧”保守降级（`LOW`），LOW 值不得参与“已认证”候选窗口生成。

## 4. 单镜窗口求解
`solve_window(req: WindowRequest) -> WindowSolution`（约束全部同时满足）：
1) 源边界：`[source_in + head_guard + in_handle, source_out − tail_guard − out_handle]`
2) 证据覆盖：窗口覆盖 `[evidence_start, evidence_end]`（或帧邻域）
3) 速度范围：`speed_min ≤ v ≤ speed_max`（按角色）
4) 时间线等式：`segment_us == audio_duration_us + tail_pad_us`
- 目标：在可行集中取 `|v-1.0|` 最小者；并列时更小的窗口优先；速度吸附到“1 帧倒数”网格，保证写入层回算后一致
- 失败原因码：`SOURCE_TOO_SHORT` `EVIDENCE_OUT_OF_RANGE` `EVIDENCE_SPAN_TOO_LONG` `EVIDENCE_SPEED_CONFLICT` `SPEED_OUT_OF_RANGE` `NO_AUDIO`

## 5. 多镜组合求解
`solve_span(req: SpanRequest) -> SpanSolution`：
- 分配等式：各段之和恰好等于 `音频+尾留白`（整数 µs，丢失的 µs 分配给余量最大的一段）
- 每段先最小份额（`MIN_SPAN_PART_US=300_000`）并夹住“证据最小时长”；剩余按各段“余量”等比分配
- 每段再调用 `solve_window`；能解视为“正常采用”，不是降级

## 6. 时序不变量校验
`check_invariants(items)` 返回违规列表：
- 源窗口几何链：`0 <= source_start < source_end <= material_duration_us`（零容差）
- 段时长计算式：`(~source/speed) ≈ segment_us`（容差 ≤1 帧）
- 口播段：`segment_us == audio_duration_us + tail_pad_us`（容差 ≤1 帧；`native` 段豁免）
- 证据覆盖：最终窗口覆盖证据区间（或帧邻域）
- 速度范围：`speed_range` 内
- 草稿回读一致性：`actual_video_us ≈ segment_us`
- 违规码：`WINDOW_OUT_OF_BOUNDS` `DURATION_MISMATCH` `EVIDENCE_NOT_COVERED` `SPEED_OUT_OF_RANGE` `APERTURE_MISMATCH`

## 7. 稳定/转场建议（`stability.py`）
- 低帧率小尺寸灰度帧 + `tblend=difference` 估算帧间差，搜索 `±search_span` 内的稳定窗口；避开黑场/静帧；必要时微调入点；给出“是否需叠化”的建议

## 8. 测试
- `test_timing_contract.py` 覆盖窗口求解/多镜/不变量/速度吸附/证据覆盖/边界容差；`test_video_decode.py`/`test_video_encode.py` 覆盖 GPU/回退链

