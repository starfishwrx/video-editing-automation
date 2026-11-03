# 子系统：媒体编解码（media-codec）

- 代码：`packages/video-v1.5.3/doubao-jianying-orchestrator/orchestrator/media/{video_decode,video_encode,ffmpeg_bootstrap,frames,action_frames,source_normalization}.py`；环境探测位于 `orchestrator/runtime/platform_env.py`

## 1. 职责
- 统一/稳健的 FFmpeg 定位与绿色引导（macOS/Windows）
- 抽帧/场景探测/视觉拼图等读取路径的“优先 GPU、失败回退 CPU”解码
- 成片 H.264 编码的“GPU 优先，级联回退（含 libx264）”

## 2. 解码（`video_decode.run_ffmpeg_decode`）
- 默认在 `-i` 前注入 `-hwaccel cuda -threads 1`（探测存在 cuda 才注入），必要时降级
- 4K HEVC 的 NVDEC 需 `-threads 1`，否则会静默回退到 CPU（stderr 有痕迹、rc=0）
- 若硬件初始化失败但进程成功：沿用这次（已退到 CPU）的结果，不重试；只有进程失败才去掉 `-hwaccel` 重跑
- GPU 使用/回退会打印一次性说明（去重）

## 3. 编码（`video_encode`）
- 首选 GPU：`h264_nvenc`（`-pix_fmt yuv420p -preset p4 -rc vbr -cq 23 -b:v 8M ...`）
- 次选 GPU：`h264_amf`/`h264_qsv`
- CPU 回退：`libx264`（优先尝试 PATH/WinGet 的“带 libx264 的 ffmpeg”）；再不行退 `h264_mf`/`mpeg4`
- 选用编码器后打印一次性说明，便于审计“是否真的用到 GPU/是否落回 CPU”

### 旋转源素材的无头写入

- 只对已选中的旋转源生成画面朝向正确的完整工作副本，不重做识别或选镜。FFmpeg 将显示矩阵应用到像素，不缩放、不改帧率、不剪短；源音轨 stream-copy，原件只读。
- 使用无损 H.264（NVENC 或 libx264 `qp=0`），保持支持的 8/10bit 4:2:0、4:4:4 像素格式及色彩字段。10bit 不走只能保留 8bit 的 H.264 NVENC；不支持的色度、位深或已探测到的额外 HDR 流元数据明确报错，不能静默降位深。帧级 HDR 元数据兼容仍须单独验证。
- 编码时沿用 demux 时间基，正规化前后逐帧核对相对 PTS 与帧数，确保包括 VFR 在内的原源窗坐标直接沿用；副本时长仍须覆盖所选窗口。缓存位于工作根 `.cache/upright-sources`，有源身份、版本、文件完整性校验、并发锁和原子发布。
- 草稿附 `prepared_sources.json` 记录原件、副本、字节身份及等速零偏移映射。独立 QC 从草稿实际引用的副本抽帧，同时绑定原件；映射改动使旧 QC 失效，任意转码文件不能冒充原分析证据。
- 完整无损副本有首次编码和磁盘成本，跨任务复用缓存可避免重复编码。本次隔离样本的33个源文件为1.32GiB，副本为12.64GiB；机械盘上的工程收集和多次完整性校验显著增加耗时。该测量不代表所有素材的体积倍率或生产吞吐。
- 当前已审核无头后端支持0.1至8倍视频速度。保留的选镜缓存若超过该范围仍会被后端拒绝；音频/旋转兼容不代表选镜或成片质量已通过，也不静默更改倍速。

## 4. FFmpeg 定位与引导（`platform_env`、`ffmpeg_bootstrap`）
- 定位顺序：显式 `FFMPEG_BIN/FFPROBE_BIN` → Skill 自带 `bin/`（绿色免安装）→ 剪映安装目录捆绑 → PATH → 常见安装目录
- Windows 缺失时可用 `cli.setup-ffmpeg` 自动下载绿色静态构建，仅抽取 `ffmpeg(.exe)/ffprobe(.exe)` 到 `bin/`，不安装不写注册表
- `environment_report()` 汇总 `ffmpeg/ffprobe` 路径、来源、依赖、草稿根、包签名与可写性

## 5. 测试
- GPU/回退链/一次性公告/编码参数兼容性：见 `test_video_encode.py`、`test_video_decode.py`
- `test_headless_media.py` 核对 8/10bit 旋转副本的实际解码像素、原声音频、帧时钟、缓存复用与来源变动反例；`test_ai_editor_qc.py` 串接副本映射、独立 QC 与最终报告消费者。

## 6. 已知坑
- HEVC 4K 解码 `-threads 1`；只看 rc 不够，要看 stderr/实际解码器
- `mpeg4` 仅用于兜底占位；正式交付应为 H.264 且剪映可打开（`-tag:v avc1`）

