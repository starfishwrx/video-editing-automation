# Working log

按 grapeot 项目脚手架维护。每天一个日期小标题；每条 bullet 只记一件事。可复用的避坑条目晋升到根目录 `LESSONS.md`（`hits >= 3` 再升硬规则）。

## Changelog

### 2026-10-26

- 跟进已合入的 #19（环审 https://github.com/starfishwrx/video-editing-automation/pull/19#pullrequestreview-5648674977）：候选帧全部硬拒（黑场 / 过曝 / 纯色）时 `select_frames` 外层 `ok=False`。视觉 `_sample_frames` 与相位 `locate` 只收 `ok` 的帧，坏帧不再进入 contact sheet；相位样本不足 2 张返回 `PHASE_FRAME_EXTRACT_FAILED`，不调用视觉模型。质量协议升为 `frame-quality-v2`（`FRAME_QUALITY_VERSION`）。视觉缓存键的 `frame_protocol.quality` 与 `phase-timing-v3` 缓存键里的 `quality` 都引用该字符串，因此 v1 观察不会被复用；相位版本号本身仍是 `phase-timing-v3`。`JY_FRAME_QUALITY=0` 仍只按抽帧成败，黑场也可以 `ok=True`。软模糊仍只标注、不硬拒。
- 批量抽帧：`Path.glob` 在 Python 3.11+ 返回生成器，布尔值恒为真，`-fps_mode` 失败且工作目录没有 `f_*.jpg` 时会跳过 `-vsync` 重试。改为 `any(work.glob(...))`：空目录改试 `-vsync vfr`，已有输出则保留第一次结果。两次都没有输出再逐帧 `extract_frame`。`JY_FRAME_BATCH=0` 或单帧请求仍走逐帧抽取。
- 长镜头 `SAMPLE_MAX` 仍为 12，与 `SHEET_TILES` 对齐（#19 已改，本轮不改数值）。覆盖用 `JY_VISION_SAMPLE_MAX`。
- 验证边界（PR #20）：uv 0.13.5 / Python 3.11.17 上 `test_frame_quality.py` 与 `test_frame_batch.py` 共 21 项通过。编排器 discover 跑了 628 项（1 项跳过）；4 项失败是 Linux 上 Windows/Mac 路径语义（品类探测、usage ledger、两则 ship rewrite），不在本次 diff。包级 99 项里另有 4 项同类路径失败。这些单测不是真实视觉观察或剪映成片验收。未改 `VERSION`，未部署。质量门打开且合格帧不足 2 张时，视觉分析仍可能回落到镜头自带 `frame_paths`（该回落不经过硬门）；没有自带帧则样本为空。单测通过不等于生产通过。
- 根据10月26日会议及用户后续确认新增[功能计划](plans/20261026_feature_plan.md)，明确先匹配音色再处理一次TTS倍速、音频驱动字幕及剪映批量调整、BGM随机与去前导空白、纸品减少重复、单稿单视频和人工成品参考。
- 记录用户确认的画面口径：2倍为偏好，最高仍允许20倍；将纸品PR #15列为待验收依赖。本次仅计划入库，运行逻辑、发布包及生产配置未改。
- 文档本地链接核对32项无缺失；沿用冻结依赖运行包级99项与编排器607项离线测试，均通过。这些检查不作为新增功能或真实成片效果的验收结论。

### 2026-10-24

- 将当前默认模型完整标为 GLM-5.3-Flash / `glm-5.3-flash`，修正视觉模块注释，补齐模型输入和进程配置说明。
- 同步默认 `ai_editor` / QC v4 的架构、产品与子系统入口，旧 Top3/场景规则/QC v3 明确限于 legacy。
- 区分维护主线 1.5.6（目录 1.5.3）、索引实验 1.5.4/1.5.5、已发布下载附件和生产安装身份。
- 新增快速开始、脱敏 manifest/PowerShell 配置模板、贡献说明、PR 模板和 Windows 离线 CI，扩充本机配置/产物忽略规则。
- 修改前在独立检出目录按冻结依赖运行基线：包级 99 项通过；编排器运行 607 项，1 项跳过，其余通过。模型与生产效果未作为本次离线测试结论。
- 修改后使用 uv 0.10.6 / Python 3.11 完整回归：包级 99 项、编排器 607 项全部通过；准备 FFmpeg 后原跳过项也执行通过。
- 快速开始实际生成 3 秒 640×360 / 25 fps 合成素材，`--no-vision` 产出 1 个镜头、2 张可读帧、0 个探测/抽帧问题；配置模板、CLI help、示例 JSON 和 workflow YAML 均已核对。
- 核对 82 个本地 Markdown 链接；AST 比较确认运行逻辑不变（仅文档字符串/CLI help 更新）；拟提交文件的有效密钥/私有路径/邮箱/手机号形态扫描为 0 命中。

### 2026-10-23

- 按鸭哥 / context-infrastructure 脚手架补齐 `docs/prd.md`、`docs/rfc.md`、`docs/working.md`、`docs/test.md` 与 `docs/README.md`。
- 将 `AGENTS.md` 与根 `README.md` 的活代码路由统一到 `packages/video-v1.5.3/`，版本叙事改为读 `VERSION`（当前 1.5.6）。
- 批量修正 `docs/` 内仍指向 `packages/video-v1.5.1/` 的路径到活包；测试口径改为 `uv` + `unittest`。
- 本机 `main` 经镜像 fast-forward 到 `origin/main`（`9a19e22`，含质量合同 / NAS / 选镜审稿等 9 个提交）；文档改动 stash 后无冲突叠回。
- 配音不留气口升为全局规则（`rules/global.json` `voice.max_gap_frames=1`），教训已记入 `LESSONS.md` L-20261023-03。

### 2026-10-22

- 选镜审稿回流：开场展示、抽数/层数、揉搓与实验组句；修复去重抢镜、分数容差、Top3 与多镜旁路。详见包内 `docs/SELECTION_REVIEW_20261022.md`。

### 2026-10-13 … 2026-10-16

- 草稿共享路径验收、生成版恢复、`tools.release_package` 合包技能。详见包内 `docs/UPDATES_20261013-16.md` 与 `docs/RELEASES.md` 的 1.5.3 条目。

## Lessons Learned

详细错题本在 [`../LESSONS.md`](../LESSONS.md)。这里只留「接手时最先要知道」的几条：

- 目录名 `video-v1.5.3` ≠ 产品版本；改版本只动 `VERSION`，不要新建 `packages/video-v1.5.6/`。
- 副本 ZIP / NAS 哈希一致，仍可能因时间轴字面路径失效而媒体丢失；交付核验走 `tools.draft_delivery`。
- 版本闸门用 `_version_key` 做下限比较，不要写成等值。
- 4K HEVC CUDA 解码必须带 `-threads 1`，只看 ffmpeg 退出码会静默退回 CPU。
- 影响选镜的字段（如 `delivery_mode`）必须在 `cli.cmd_build` 侧先写入 manifest，引擎内归一化对 shot_analyzer 无效。
- 气口等跨品类口径改 `global.json`；不要只改卫品包然后以为全品类生效。
- 抽帧硬门全拒时外层 `ok` 必须为假；只看 `ok` 的调用方不得把坏帧送进接触表。逃生阀是 `JY_FRAME_QUALITY=0` / `JY_FRAME_BATCH=0`。判断目录里有没有输出要用 `any(path.glob(...))`，不要对 `Path.glob` 取布尔值。协议变更升 `FRAME_QUALITY_VERSION`，否则视觉 / 相位缓存会复用旧观察。
