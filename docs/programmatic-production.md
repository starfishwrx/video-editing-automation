# 独立时间线与无草稿工作预览

本页记录[程序化生产方案](plans/20261101-programmatic-production.md)第一阶段的实际实现。输入为固定选镜和已经准备的配音，输出为独立 `timeline.json`、分轨音频、工作预览和三类检查结果。生产核心不导入 JyProject、不查草稿根、不调用选镜、TTS 或业务队列。

1.6.0 默认 Windows 制作复用现有选镜、配音和对齐，在草稿写入阶段调用无头后端，空 BGM 自动选曲，接通原有独立 QC、NAS 分发与版本恢复，见 [默认生产入口](windows-pipeline.md)。本页的独立 `EditTimeline` 渲染／待审交接仍为显式入口；技术渲染成功保持 `UNCERTIFIED`。完整独立内容模型、观看验收、原生 MP4 导出验收及纸品动作默认接入仍待实施，纸品 34 揉搓的历史不合格结论不变。

## 命令

在活包 `packages/video-v1.5.3` 执行。`$selectedManifest` 为包含固定 `segments` 的 JSON；`$newReport`、`$newRender` 为新的包外目录。

```powershell
uv run --frozen python -X utf8 -m tools.production prepare --input $selectedManifest --report-dir $newReport
uv run --frozen python -X utf8 -m tools.production render --timeline "$newReport/timeline.json" --output $newRender --width 540
uv run --frozen python -X utf8 -m tools.production check --timeline "$newReport/timeline.json" --render-dir $newRender
```

`preview --input $selectedManifest --report-dir $newReport --width 540` 顺序执行准备、渲染与检查。FFmpeg/ffprobe 来自命令参数、`FFMPEG_BIN`/`FFPROBE_BIN` 或 PATH；不要求剪映安装。没有准备好的配音时明确阻断，不启动 TTS。

可重复的合成演示从正常入口运行，不使用真实商品或人工标签：

```powershell
uv run --frozen python -X utf8 -m tools.production_demo --output $newDemo
```

演示包含两句固定时间、三路独立音轨、中文字幕与受保护的真实末帧。音轨是合成音，不是口播质量样本。原 `tools.selection_preview --draft ...` 保留历史对照；新增 `--timeline ... --output ...` 进入相同的独立渲染器。

## 输入与支持范围

固定 manifest 沿用 `segments[].video/audio/tts_path/audio_mode/text/source_start/source_end/video_speed`。`span_parts` 同时认 `segment_s` 和 `speed`，每句音轨与字幕只生成一次。Clip 适配优先使用 `raw_index` 保留原句身份，不用文本作字典键。已有多镜分配与最终音频时长不符时要求重解，不按字数或比例伪造子段时长。

| 内容 | 当前执行范围 |
|---|---|
| 时间与身份 | 整数微秒；句、镜头、字幕各自 ID；严格字段和引用校验；修订改变后旧渲染失效 |
| 视频 | 单层硬切；CFR；恒速；cover/contain、放大及位置；不透明画面 |
| 帧 | 源帧/真实 PTS 选取并核验；内部切点累计 floor，整片末端 ceil，保留小于一帧的画面余量；拒绝补尾帧、编码增删帧和不足的保护帧 |
| 音频 | 缓存 TTS 的最终响度处理；操作方 file 音频保持；真实最终时长；分开的 narration/BGM/original WAV；增益、基础淡入淡出、48k 样本延迟 |
| 字幕 | 原句文字去标点；独立 ID 和事件；明确像素字号、归一位置、字体、颜色、粗体；libass 基础布局代理 |
| 暂不支持 | VFR、旋转/非方形像素、需要重复源帧的慢速/升帧组合、任意帧表/分段变速、复杂效果/转场、原声变速或音画流起始偏移、自动 BGM 搜索或补长 |

`AudioEvent` 的源长度必须真实够用，静音只用于铺满整条音轨的未占用位置；不能掩盖短旁白。单镜以处理后的实际音频决定时间；多镜沿用已有分配并校验总长。改变最终音频后不把旧的 `audio_duration_us` 当作真值。

新合同的能力声明是当前渲染器的受限输入范围；剪映端的表达和可编辑性仍待方案 PR4 验证。字幕使用 libass 的字体度量与百分之一秒时间量化，相邻字幕两端使用同一量化规则。小于该量化单位的字幕，以及直接时间线中的字面反斜杠/花括号，明确拒绝。不据此认定剪映原生字体、换行和复杂效果已保真。

## 输出与检查

- `timeline.json`：独立编辑结果，不接受 `status: PASS` 等认证字段；不含草稿 JSON。
- `prepared_assets.json`：最终配音、实际时长、处理与媒体事实。
- `render/preview.mp4`、三路 WAV、`mix.wav`、`subtitles.ass`：可播放和分别审查的媒体。
- `render/render_receipt.json`：成功后最后落盘，含实际解码 PTS、输出帧映射、保护帧数量、量化差和当前资产身份。失败目录只有明确阻断记录，不能覆盖旧成功输出。
- `render/production_qc.json`：结构媒体、内容语义、观看效果分别记账。当前执行结构媒体检查；时间线已有未解决内容需求时，内容项明确 `BLOCKED`，不混记为媒体损坏。其余内容与观看检查 `NOT_RUN`，总状态始终未认证或阻断；导出保真同样待做。

这些输出尚不进入现有 `tools/acceptance` 的四项正式验收，也不会生成完成回写。保留现有实际草稿 QC 和正式交付边界，不能把该检查结果改名成旧草稿 QC 的 PASS。

## 验证与接续

新增合同、准备和真实 FFmpeg 测试覆盖重复句、多镜共享旁白、新报告目录、实际音频与旧分配冲突、未支持效果、源窗和音频错误、非零 PTS、实际输出逐帧像素对应、结果帧缺失和修订失效。合成媒体可以证明执行合同，不证明商品识别、动作完整性、口播质量或专业编辑接受。

方案 PR1 的固定输入数据与资产边界、PR2 的渲染与检查骨架已落地；一份人工确认的真实固定选镜集成、独立内容/观看验收仍待补。接续按原计划推进 PR3 动作/语义接入、PR4 可编辑导出和 PR5 调度恢复，默认切换与部署另行执行。

## 显式无头剪映待审交接

`tools.production headless-plan --timeline $timeline --render-dir $render --task-json $task --bgm-state-dir $selectionState --name $newName --font $localStaticFont --report-dir $newPlan --review-only` 先补齐业务音频，再将固定结果转换成 `jy14-headless-plan/v1`。任务和选曲状态目录为必填项，空 BGM 沿用 worker 的自动选曲合同；已有标题/ID/路径优先，不能把空字段当成关闭音乐。纯数据层 `headless_plan` 保留给明确准备好的技术输入。

业务阶段使用现有曲库、任务选择记录、批次去重及 BGM 响度处理。任务身份和文案必须对应原时间线，不重新选镜或合成旁白；只替换独立 BGM 事件并增加修订。歌曲按所用窗口准备到默认 -28 LUFS、0.4秒淡入/0.2秒淡出，淡变一次烘焙到最终48k PCM，物理样本数向上取整而事件保持原微秒长度。实际音频源必须足够覆盖去静音/指定起点后的全片长度，禁止用长静音补成成功；不支持带视频的 BGM 源。任务选择记录可跨重试复用，批次身份兼容 worker 的三个字段。

输出为 `$newPlan/timeline.json`、`bgm/bgm_receipt.json`、`render/preview.mp4` 及 `export/plan.json`。保留原视频、旁白、字幕与内容疑点，重新渲染同一画面和新增音乐，之后从当前修订导出。一次视频可承接多句，重复字幕仍各自保留。当前限1080×1920/30fps、同画幅居中硬切、1–8倍恒速、基础音轨及默认字幕；事件级淡变仍不直接映射到无头底座，预处理后的音乐不再次叠加淡变。未支持的布局、同角色重叠或字幕样式明确拒绝。

外部 `jianying-headless-windows` 通过自己的固定版本、DLL 与签名检查，使用 `build --detached-root $approvedNasParent` 构建原生加密副本。这个待审交接扩展需要对应后端版本，不能只用旧版 build 冒充。媒体与静态字体由后端收集，活动时间轴与镜像由本机原生 codec 生成；不手改草稿 JSON、不登记本机首页、不控制界面。新建文字显式映射 bold；原生字号单位与选定本地字体不宣称和 libass 代理像素一致。

已获用户授权复制待审稿时，`tools.headless_review_delivery --build $build --headless-root $backend --timeline $timeline --report-dir $newReceipt --publish` 使用现有原子复制工具发布独立 NAS 主目录与分发副本；不指定 `--publish` 只读验证。工具读取实际加密时间轴及5个镜像，比较文字、源窗、倍速、位置、音轨和微秒时序，并核对共享目录的字面媒体与字体引用。旧的明文 NAS 工具不支持这个原生加密格式，不用旧 sidecar 替代它。

这是有明确授权的待审副本交接，不接入自动队列或正式 finalizer。收据始终 `formal_delivery_allowed=false`；三项内容/观看/原生显示验收未完成时保持待审。只有结构、包和参数通过，不能把业务表标成正式完成或已验收。业务回填只写请求范围内的草稿链接和真实问题，保留原内容疑点；不新增人工批准文件。收到最终草稿后仍需编辑侧开稿/播放/保存重开验收。
