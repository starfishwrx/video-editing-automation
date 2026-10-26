# 子系统：剪映草稿写入（jianying-editor）

- 代码：`packages/video-v1.5.3/jianying-editor/scripts/**`
  - 核心：`core/{media_ops.py,text_ops.py,vfx_ops.py,project_base.py}`
  - 工具：`utils/{formatters.py,media_normalizer.py,cli_protocol.py,...}`
  - 包装：`jy_wrapper.py`（供 orchestrator 动态导入）
  - 第三方：`vendor/pyJianYingDraft/**`（只读，不改）

## 1. 职责
- 把“编排层的段落计划”安全、可审计地写成剪映草稿（可被桌面客户端打开/播放/导出）
- 生成草稿骨架、添加视频/音频/字幕轨、落入/出点、样式/位置与可见标注
- 写完后回读验证结构与媒体引用，并同步 `root_meta_info.json`

## 2. 写入与安全
- 写前：通过 orchestrator 的 `draft_safety.pre_write_check` 做“是否覆盖/是否加密/是否可写”防线
- 写后：`draft_safety.post_write_validate` 复读草稿，校验
  - `draft_info.json`/`draft_meta_info.json`/（必要时）`Timelines/**/project.json` 一致性
  - 视频轨/旁白轨/BGM/字幕轨段数；字幕字号/粗体/位置（`X=0,Y=-0.55`）等默认兜底与规范
  - 引用的 `media/` 文件存在；音频轨不超过视频结尾

会议候选通过维护 wrapper 的 `add_caption_cues` 写统一原生 `subtitle` 素材轨；按每句最终 TTS 时长生成的原文字幕与草稿文本/区间独立回读。无头回读能验证结构和媒体，剪映全选移动与保存重开需另留UI验收，详见[实现记录](../meeting-features-20261026.md)。

## 3. 运行与定位
- 由 orchestrator 使用 `platform_env.find_jianying_editor_root()` 定位；优先 `JY_SKILL_ROOT`，否则搜索常见技能根
- Windows 场景：`uiautomation` 仅在 pyJianYingDraft 构造草稿时可能导入；macOS 不要求

## 4. 版本与兼容
- `packages/video-v1.5.3/jianying-editor/VERSION`、`SKILL.md` 为本组件元数据；不绑定业务规则
- 与 orchestrator 的“素材总长/µs 口径/速度吸附”严格对齐，避免写入后产生错位

## 5. 测试
- 写稿后回读校验、字幕样式兜底、BGM 对齐、根索引同步，涵盖在 orchestrator 的端到端测试与 `draft_safety` 校验中

