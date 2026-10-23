# Working log

按 grapeot 项目脚手架维护。每天一个日期小标题；每条 bullet 只记一件事。可复用的避坑条目晋升到根目录 `LESSONS.md`（`hits >= 3` 再升硬规则）。

## Changelog

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
