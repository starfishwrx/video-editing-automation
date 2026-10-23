# 子系统：媒体编解码（media-codec）

- 代码：`packages/video-v1.5.3/doubao-jianying-orchestrator/orchestrator/{video_decode.py, video_encode.py, ffmpeg_bootstrap.py, platform_env.py}`

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

## 4. FFmpeg 定位与引导（`platform_env`、`ffmpeg_bootstrap`）
- 定位顺序：显式 `FFMPEG_BIN/FFPROBE_BIN` → Skill 自带 `bin/`（绿色免安装）→ 剪映安装目录捆绑 → PATH → 常见安装目录
- Windows 缺失时可用 `cli.setup-ffmpeg` 自动下载绿色静态构建，仅抽取 `ffmpeg(.exe)/ffprobe(.exe)` 到 `bin/`，不安装不写注册表
- `environment_report()` 汇总 `ffmpeg/ffprobe` 路径、来源、依赖、草稿根、包签名与可写性

## 5. 测试
- GPU/回退链/一次性公告/编码参数兼容性：见 `test_video_encode.py`、`test_video_decode.py`

## 6. 已知坑
- HEVC 4K 解码 `-threads 1`；只看 rc 不够，要看 stderr/实际解码器
- `mpeg4` 仅用于兜底占位；正式交付应为 H.264 且剪映可打开（`-tag:v avc1`）

