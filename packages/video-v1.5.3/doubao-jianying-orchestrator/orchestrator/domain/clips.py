"""Shared clip state; no orchestration, media I/O or external services."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

@dataclass
class Clip:
    video: str
    source_start: float
    duration: float
    text: str = ""
    claim_text: str = ""
    temporary_shot_id: Optional[str] = None
    audio: Optional[str] = None
    audio_mode: str = "tts"          # tts / native / mute / file
    voice: Any = None
    visual_requirements: Any = None
    evidence_tags: Any = None
    visual_tags: Any = None
    evidence_intervals: Any = None
    rationale: str = ""
    action_complete: Optional[bool] = None
    head_waste: float = 0.0
    tail_waste: float = 0.0
    transition: Optional[dict] = None
    stability: Optional[dict] = None
    tts_path: Optional[str] = None
    start_us: int = 0
    # v1.3.25：该段是「素材库没有能证明这句口播的镜头」时的兜底采用。
    # 成片里必须打可见标注，不允许它冒充正常镜头（用户定案 2026-09-16）。
    degraded: bool = False
    # 预览阶段严格匹配无可用画面时的显式黑片段；只占画面轨，旁白/字幕仍正常写入。
    visual_missing: bool = False
    visual_missing_note: str = ""
    # ── 时序契约（v1.3.27，用户 2026-09-16 定案第 3~6 条）──────────────────
    # 规划阶段（shot_analyzer）已用**真实音频时长**解出可行窗口并写进段落；
    # 引擎只执行这个解、只校验这个解，**不再自己另算一套长度**。
    source_end: float = 0.0            # 段落声明的源窗口右界（秒）
    audio_duration_us: int = 0         # 规划时用的真实音频时长（µs）
    timing_certainty: str = ""         # audio / estimated / segment
    video_speed: float = 1.0           # 求解给出的变速（1.0 = 原速）
    window_solution: Any = None        # 规划阶段的完整解（审计用）
    # 引擎对齐时**自己解出来的**解（`align_audio_video` 用真实音频时长重解一次，
    # 定案第 3 条口径统一）。写入前的不变量校验以此为基准；用吸附后的浮点值反推
    # 等于自证，发现不了求解器本身给错解的情况。
    solved_window: Any = None
    window_reason: str = ""            # 求解失败时的结构化原因码
    # 画面角色 + 是否承载时间相关的直接证据：决定变速范围（定案第 6 条）。
    visual_role: str = ""
    time_sensitive: bool = False
    # v1.5.2 场景规则（卫品指令）显式给出的变速范围，优先于画面角色的范围。
    speed_range_override: Any = None
    preferred_speed: float = 1.0
    playback_observation: Any = None
    continuity_group: Any = None
    # 场景规则按动作相位定窗时的头尾保护区（秒），优先于 manifest 的 head/tail_trim_s。
    guard_override: Any = None
    # 转场把手（定案第 5 条：与首尾废料保护区**分开**扣）。转场会吃掉紧邻切点的
    # 若干帧，这段素材不能被「装配音」用掉。0 表示该侧没有转场，不扣。
    transition_in_handle_us: int = 0
    transition_out_handle_us: int = 0
    # ── 多镜段的展开记账（v1.3.27 B 步）───────────────────────────────────
    # 摊平后每个子段要知道自己「属于哪条原段落、是第几段」，以及**旁白是谁的、
    # 该取哪一段时间片** —— 旁白只有一份，不能每段都从头播一遍。
    parent_index: int = -1     # 所属原段落下标（子段为其父，单镜段为自己）
    # 在 manifest `segments` 里的原句下标。同一实验镜头可分给连续几句，镜头号不能当句子身份。
    raw_index: int = -1
    span_index: int = 0        # 在原段落里的第几段（单镜恒为 0）
    span_total: int = 1        # 原段落被拆成几段
    audio_clip_index: int = -1  # 旁白归属：哪条原段落的音频（决定 TTS 缓存归属）
    # 父段落的旁白铺在**输出时间线**上的起点（µs），由 ``_mark_audio_origin``
    # 在 ``align_audio_video`` 推进游标时，按父段落**首次出现**建立。
    # 各子段要取的那一片，在配音里的偏移 = 「本段 `start_us`」− 本字段 ——
    # 本字段是**基准**，偏移是老式相减的结果，两者不要搞反。
    # 用「本段起点 − 父段起点」而不是「各兄弟段长累加」，是为了不与游标推进方式
    # 耦合 —— 正确解上两者恒等，但耦合会在游标算法一改就静默错位。
    # 单镜段恒等于自己的 `start_us`（偏移 0，整句从头播）。
    audio_clip_start_us: int = 0
    track_name: str = "Video_BRoll"
    # 运行期
    video_duration: float = 0.0
    # `stabilize` 选出的稳定入点（**提示**，不是边界）。
    #
    # v1.3.27（用户定案第 3 条「统一口径」）：这个值只能当 `solve_window` 的
    # `preferred_start_us` 用。v1.3.24~26 是直接覆写 `c.source_start`，等于把
    # 稳定性结果变成了**素材区间下界** —— 而规划层（`shot_analyzer.candidate_window`）
    # 用的是镜头自己的 `source_start`。两个区间一旦不同，就会出现两种翻案：
    # 稳定入点后移 → 规划批准的解在引擎侧变成 SOURCE_TOO_SHORT（规划说行、交付才炸）；
    # 前移 → 引擎求出规划层判死的解（静默换解）。所以 `source_start` 保持规划值不动，
    # 稳定入点只做提示，由求解器在**同一个可行集**里挑落点。
    stable_start: Optional[float] = None
    # 对齐时 ffprobe 出的**真实音频时长**（µs）。写入前不变量校验必须拿它做
    # 「段长 = 音频 + 尾留白」的比对基准；拿 c.duration 自己比自己等于没校验。
    runtime_audio_us: int = 0
    # ── 多镜组合（v1.3.27 B 步，用户定案第 6 条）──────────────────────────
    # 一句话装不下一个镜头时，规划层（`shot_analyzer.plan_span`）把**同一句配音**
    # 按时间切成 N 段，每段配一个镜头。规划层的原始分段表在**段落字典**的
    # `span_parts` 键里（那是 `raw`，不是 Clip），摊平后**不搬到 Clip 上**：
    # 每段 Clip 自己就带着本段的解（`solved_window` / `window_solution`），
    # 旁白偏移由 `audio_clip_start_us` 相减得出。曾经在这里留过
    # `span_parts` / `span_audio_offsets_us` 两个字段，但从来没有任何生产者
    # 写它们 —— 恒为 None / 0 的字段比没有更糟：读的人会以为拿到的是数据。
    #
    # 口径等价性（这是多镜不必放宽任何阈值的原因）：组合**不引入新的时间口径** ——
    # 只是把同一份音频按时长拆开，再对每段调用**同一个** `solve_window`。
    # 「每段窗口合法」+「各段段长之和恰好等于 audio + tail_pad」两条合起来，
    # 与单镜那条不变量完全等价。所以 `check_invariants` 的每条阈值都不用动，
    # 只要把多镜展开成**每段一条** item 去逐条校验。
    #
    # 单镜路径下 `multi_shot=False`，行为与 v1.3.26 一致。
    multi_shot: bool = False
