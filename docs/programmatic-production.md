# 独立时间线与无草稿工作预览

本页记录[程序化生产方案](plans/20261101-programmatic-production.md)第一阶段的实际实现。输入为固定选镜和已经准备的配音，输出为独立 `timeline.json`、分轨音频、工作预览和三类检查结果。生产核心不导入 JyProject、不查草稿根、不调用选镜、TTS 或业务队列。

默认 Windows 制作路径保持现状。新路径必须显式运行；技术渲染成功保持 `UNCERTIFIED`，不能进入正式交付。自动纸品动作接入、独立内容模型与观看验收、剪映末端导出、调度恢复尚未实施。纸品 34 揉搓的历史不合格结论不变。

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
- `render/production_qc.json`：结构媒体、内容语义、观看效果分别记账。当前只执行结构媒体检查，另外两项 `NOT_RUN`，总状态始终未认证或阻断；导出保真同样待做。

这些输出尚不进入现有 `tools/acceptance` 的四项正式验收，也不会生成完成回写。保留现有实际草稿 QC 和正式交付边界，不能把该检查结果改名成旧草稿 QC 的 PASS。

## 验证与接续

新增合同、准备和真实 FFmpeg 测试覆盖重复句、多镜共享旁白、新报告目录、实际音频与旧分配冲突、未支持效果、源窗和音频错误、非零 PTS、实际输出逐帧像素对应、结果帧缺失和修订失效。合成媒体可以证明执行合同，不证明商品识别、动作完整性、口播质量或专业编辑接受。

方案 PR1 的固定输入数据与资产边界、PR2 的渲染与检查骨架已落地；一份人工确认的真实固定选镜集成、独立内容/观看验收仍待补。接续按原计划推进 PR3 动作/语义接入、PR4 可编辑导出和 PR5 调度恢复，默认切换与部署另行执行。
