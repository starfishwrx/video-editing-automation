# 子系统：音频与音色（audio-voice）

- 代码：`packages/video-v1.5.3/doubao-jianying-orchestrator/orchestrator/audio/{voice_tts,voice_catalog,voice_library,library_resources,bgm_selector,audio_loudness,audio_captions,voice_request}.py`；文本审核位于 `orchestrator/policy/script_polish.py`

## 1. 职责
- 口播音色解析/推荐/唯一决议，批量 TTS 合成与缓存，试听样本生成
- 旁白响度归一（-16 LUFS，纯增益 + 真峰值限幅，固定输出时长）
- BGM 推荐/下载/铺底处理（整体响度 -28 LUFS，首尾淡变，APAD 对齐）
- 会议候选的统一音色/倍速输入、原文与最终 TTS 音频时长驱动的字幕、BGM任务收据见[实现记录](../meeting-features-20261026.md)。入口增加 `voice_request.py`、`audio_captions.py`；先锁音色再处理一次倍速，空独立字段不屏蔽名称后缀，失败不假装已生效。

## 2. 音色解析与歧义处理
- 统一入口：`voice_catalog.resolve_voice_once(spec, script, manifest, persist=True)`
  - 若 `manifest["voice"]` 已填：严格解析，不静默换音色；倍速写法如“猴哥1.6倍速”会被拆解并随 preset 返回
  - 为空：用 `recommend_voices`（结合脚本/人设与全库画像）取首选并写回 `manifest["voice"]`，同时写 `voice_locked_from`
  - 再不命中：退回默认音色 `male_energetic`
- 解析失败抛 `VoiceResolutionError` 并返回候选清单给运营选择（不自动猜测）
- “全库”来源：`voice_library.load_library()`（官方显示名 CSV + 历史别名），并做中文性别/风格画像与“排除方言/外语/角色/特效音”（除非剧情明确需要）
- CSV 位于 `jianying-editor/resources/catalogs/`，随 Git 和源码 ZIP 交付；不从本机 `data/` 隐式补表。`TVB女声1.2` 对应 `BV409_streaming`、1.2 倍速，`TVB女声Pro` 是另一音色。缺失／坏表是 `ENV_ERROR / RUNTIME_CATALOG_*`，与名称歧义分开；应用层生产预检在付费前汇总资源状态。
- dispatcher 按 worker 相同的会议字段规范化预检音色、原文回填及独立倍速；无效输入只阻断本任务，其他任务继续，QC 恢复不重做音频准入。空音色保留自动选择合同，但仍要求资源就绪；显式本地 BGM 不要求音乐表。直接 `build` 也在配音／分析前检查所需表。

需要人工核对本机剪映音色面板时，可在活包根显式运行以下只读快照工具，输出必须是新文件：

```powershell
$voiceCache = Join-Path $env:LOCALAPPDATA 'JianyingPro\User Data\Cache\ressdk_db'
uv run --frozen python -m tools.extract_voice_catalog --cache-root $voiceCache --output "$env:TEMP\voice-review-snapshot.csv"
```

工具拒绝标记仍有后续页的响应、冲突数据及可能被表格软件当作公式的单元格；仅凭缓存无法确定是否从首页取到全部数据，因此快照不保证完整。数据库使用 SQLite 只读连接；不覆盖已有文件，也不允许写入缓存目录或随包音色表目录。输出是五列待审快照，不是运行时音色表，不会自动启用；名称和标签可能含本机信息，分享前须自行核对。

## 3. TTS 并发与缓存
- 缓存键仅看“文本+音色+后端+后处理版本”（`AUDIO_PIPELINE_VERSION`），与 `video_speed/窗口` 无关；命中即完全复用字节
- 后端优先 `SAMI`（需联网）；失败不自动回落到 Edge（会造成“所听非所选”），而是显式报错；长文本损坏时自动“分段合成+拼接”恢复
- 试听：固定样词，默认 TopN；生成 MP3 并带 `.backend` 标记；复用缓存避免重复合成

## 4. 旁白响度归一（`audio_loudness.normalize_narration`）
- 目标 -16 LUFS（EBU R128）；先测 Integrated LUFS，再计算增益；用 `alimiter` 钉 -1.5 dBTP 峰值
- 内部最终旁白统一为 48kHz、双声道 PCM WAV，以源时长对应的整数采样数执行 `apad+atrim`，保留既有 20/30ms 微淡变。避免原 48kHz TTS 转为 44.1kHz AAC 后出现微秒级边界差；不修改选镜句长。
- 写稿保持完整音频、切片起点和时间线连续性检查；仅对 48kHz PCM 接受取整后属于同一个采样边界的微秒差，不允许真实少一个采样或缺失尾音。
- 处理缓存键包含源文件身份、独立归一版本、采样率、编码与响度参数。旧响度资产失效，原始云端 TTS 缓存键不变；同路径加锁，校验 PCM 实际数据长度后原子发布，损坏的缓存自动重建。
- `draft_write` 恢复复用原配音字节，记录其 TTS 来源后继续归一；旧版已绑定为 file 的报告从已核对的原始输入及逐句 key/text 补回来源。操作方原本提供的文件音频保持既有规则。
- 失败/极端输入（< -50 LUFS）明确阻断并记录，不把未归一资产交给写稿层。

## 5. BGM 推荐与铺底（`bgm_selector`）
- 情绪规则：脚本/剧情关键词 → 情绪标签（轻快/舒缓/动感/可爱/大气/治愈/国风），并兼容英文风格词（categories 为空时用标题匹配）
- 选曲：按情绪/显式关键词/随包曲库元信息综合打分；只交付 ID、名称、时长和分类，不交付临时直链。下载固定使用当前包 CloudManager 及同一元数据索引，完整本机缓存优先，未命中或强制刷新才按 ID 实时取链。显式本地 BGM 不依赖音乐索引。
- 自动选曲仅在候选下载或明确的源音频损坏、静音、长度不足时，在有限候选内替换；显式指定曲目和已确认曲目不自动替换。按任务锁串行准备并确认成功选择，收据记录尝试曲目；FFmpeg/ffprobe 工具或输出环境故障保持阻断，不作为坏曲遍历。
- 铺底：裁剪到成片总长 → 归一到 -28 LUFS（比旁白低 ~12 LU）→ 首尾淡变与 APAD 固长 → 闭环复测响度，必要时二次纯增益修正

## 6. 脚本润色（`script_polish`）
- `review-script` 阶段生成“口播确认单”：逐句字数/预估时长/能否塞进画面 + 视觉证据体检，供运营在合成前确认文本

## 7. 测试
- 播放链/缓存键/分段恢复/响度测量与限幅/固定时长，见 `test_video_encode.py`、`test_task_control.py` 等
- `test_headless_media.py` 用真实非整毫秒音频验证 48kHz 采样边界、旧格式/截断缓存重建及旧恢复来源迁移；包级 `test_headless_media_coldstart.py` 在空用户目录和禁网的新解释器处理真实音频与旋转素材。
- `test_runtime_catalogs.py` 覆盖真实音色、倍速、缓存后删表／坏表、修复和付费前阻断；包级 `test_runtime_resources.py` 只复制 Git 已跟踪文件（解压包使用交付库存），在无用户配置的新解释器内禁止联网，验证音色及自动 BGM 的下游 ID 索引。

## 8. 已知坑
- 不要把 `video_speed` 混进 TTS 缓存键（只作用于画面）
- Edge 兜底只能在“试听/离线环境”显式启用；正式合成保持音色一致性


## 9. 纸品最终配音与五帧验收

纸品新TTS在原始合成缓存后只做一次tempo，随后归一响度、以48kHz双声道PCM做一次保守首尾修复，再给选镜和字幕定长。跳过旧 -50dB 预裁剪；最终边缘按 -40dB 连续采样定位，各留25ms，每侧最多裁0.5s且保留至少60%时长；修复不动内部停顿。全静音、损坏或保护不满足都阻断。普通显式文件音频不自动处理；voice_edge_trim=false明确禁用裁剪，最终五帧闸门仍执行。

实现为 audio/narration_preparation.py；原始TTS缓存不变，tempo/trim派生p2包含源内容及精确参数，最终paper-narration-v2缓存包含源内容、响度版本/参数和边缘策略，原子发布并校验文件与长度。选镜前报告 narration_preflight_report.json，最终入轨切片报告 narration_gap_report.json，不能用早期通过替代最终验收。

保留TTS在选镜前先准备固定句长的响度结果，保存在内部 _paper_fixed_narrations 映射，选择与写稿消费同一文件；响度禁用则保持原件。恢复已冻结句长不自动缩短、重新合成或再次变速。最终资产不重复归一；检查后字节变化阻断。旧音轨仍超限时明确要求重排，保留旧选镜和原件。

连续尾静音不能包含静音结束后重新出现的有声尾音；FFmpeg零起点日志异常仅允许由明确结束时间及静音时长恢复。检测故障与真实超限分别分类，详见 [ADR 0010](../adr/0010-final-narration-gate.md)。采样能量保护不能代替首尾听审。
