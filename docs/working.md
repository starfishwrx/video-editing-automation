# Working log

按 grapeot 项目脚手架维护。每天一个日期小标题；每条 bullet 只记一件事。可复用的避坑条目晋升到根目录 `LESSONS.md`（`hits >= 3` 再升硬规则）。

## Changelog

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
