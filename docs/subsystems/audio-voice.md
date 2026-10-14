# 子系统：音频与音色（audio-voice）

- 代码：`packages/video-v1.5.1/doubao-jianying-orchestrator/orchestrator/{voice_tts.py, voice_catalog.py, voice_library.py, bgm_selector.py, audio_loudness.py, script_polish.py}`

## 1. 职责
- 口播音色解析/推荐/唯一决议，批量 TTS 合成与缓存，试听样本生成
- 旁白响度归一（-16 LUFS，纯增益 + 真峰值限幅，固定输出时长）
- BGM 推荐/下载/铺底处理（整体响度 -28 LUFS，首尾淡变，APAD 对齐）

## 2. 音色解析与歧义处理
- 统一入口：`voice_catalog.resolve_voice_once(spec, script, manifest, persist=True)`
  - 若 `manifest["voice"]` 已填：严格解析，不静默换音色；倍速写法如“猴哥1.6倍速”会被拆解并随 preset 返回
  - 为空：用 `recommend_voices`（结合脚本/人设与全库画像）取首选并写回 `manifest["voice"]`，同时写 `voice_locked_from`
  - 再不命中：退回默认音色 `male_energetic`
- 解析失败抛 `VoiceResolutionError` 并返回候选清单给运营选择（不自动猜测）
- “全库”来源：`voice_library.load_library()`（官方显示名 CSV + 历史别名），并做中文性别/风格画像与“排除方言/外语/角色/特效音”（除非剧情明确需要）

## 3. TTS 并发与缓存
- 缓存键仅看“文本+音色+后端+后处理版本”（`AUDIO_PIPELINE_VERSION`），与 `video_speed/窗口` 无关；命中即完全复用字节
- 后端优先 `SAMI`（需联网）；失败不自动回落到 Edge（会造成“所听非所选”），而是显式报错；长文本损坏时自动“分段合成+拼接”恢复
- 试听：固定样词，默认 TopN；生成 MP3 并带 `.backend` 标记；复用缓存避免重复合成

## 4. 旁白响度归一（`audio_loudness.normalize_narration`）
- 目标 -16 LUFS（EBU R128）；先测 Integrated LUFS，再计算增益；用 `alimiter` 钉 -1.5 dBTP 峰值
- AAC 帧对齐造成的 -6.5ms 统一用 `apad+atrim=duration=D` 固定到源长；并加 20/30ms 微淡变消除硬切爆点
- 失败/极端输入（< -50 LUFS）安全退出并记录

## 5. BGM 推荐与铺底（`bgm_selector`）
- 情绪规则：脚本/剧情关键词 → 情绪标签（轻快/舒缓/动感/可爱/大气/治愈/国风），并兼容英文风格词（categories 为空时用标题匹配）
- 选曲：按情绪/显式关键词/曲库元信息综合打分；优先带直链；支持试听切片缓存
- 铺底：裁剪到成片总长 → 归一到 -28 LUFS（比旁白低 ~12 LU）→ 首尾淡变与 APAD 固长 → 闭环复测响度，必要时二次纯增益修正

## 6. 脚本润色（`script_polish`）
- `review-script` 阶段生成“口播确认单”：逐句字数/预估时长/能否塞进画面 + 视觉证据体检，供运营在合成前确认文本

## 7. 测试
- 播放链/缓存键/分段恢复/响度测量与限幅/固定时长，见 `test_video_encode.py`、`test_task_control.py` 等

## 8. 已知坑
- 不要把 `video_speed` 混进 TTS 缓存键（只作用于画面）
- Edge 兜底只能在“试听/离线环境”显式启用；正式合成保持音色一致性

