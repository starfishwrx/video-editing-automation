# 字幕安全区与居中

默认所有字幕水平居中，每行文字也居中；这不是把字幕移到画面正中央。沿用现有下方锚点 `x=0, y=-0.55`。默认安全区为画布四边各留 10%，字幕文字与描边都应位于内侧 80% 区域。

长句按字体实际字宽换行，优先少行并平衡行宽，英文尽量不断词，保留已有换行。只插入显示换行，不修改原文、配音、字幕开始结束时间。多行高度超出安全区时调整位置；极长内容需缩字号才放得下时按实际高度缩小，仍不能在可读字号下容纳则明确失败，不截字。

共享几何实现是活包 `jianying-editor/scripts/utils/subtitle_layout.py`；编排器通过 `media/caption_layout.py` 仅加载这个纯函数。FFmpeg ASS、默认无头生产、时间线导出和维护中的 JyProject 字幕入口均使用它。富文本按最大高亮字号保守计算布局，插入换行后重映射高亮区间。标题等非字幕轨不强制改样式。

无头计划明确写入 `subtitle=true, alignment=1, max_line_width=0.8`，底层原生素材写 `alignment=1, line_max_width=0.8, force_apply_line_max_width=true`，并回读验证。旧 JyProject 保留底层序列化格式，通过显式换行及逐条布局检查验证。不能只看配置值宣称实际画面通过。

## 后端依赖

默认无头写入需要后端 `production_library.TEXT_LAYOUT_SCHEMA = centered-safe-subtitles/v1`。缺少该能力会在生产预检时明确阻断，避免完成付费分析/配音后才发现计划参数不支持。此依赖属于独立的 jianying-headless 工程，不能只更新视频包而继续使用未升级后端。

本次后端变更在隔离分支 `fix/subtitle-safe-area`（本地提交 `3fe525ce7075c6e8754c2de2a267d1cdeb386995`，尚未发布）：新增两项有范围校验的文本参数、原生写入与回读检查，并按实测更新原有源码 pin。未修改正在使用的安装目录。部署时须同时安装经过验证的后端，并设置 `JIANYING_HEADLESS_ROOT`；开发验证不切换默认生产。

## 验证

专项：`test_caption_layout.py`、`test_meeting_caption_integration.py`、`test_default_headless.py`、`test_timeline_export.py`、`test_timeline_render.py`；跨共享合同修改还运行两套全量与架构检查。覆盖中文长句、中英混排、字宽差异、多行高度、横竖屏、安全位置、高亮偏移与逐项样式回读。

真实字体的 FFmpeg 渲染与无头草稿参数回读分别记录。二者通过仍不能代替剪映原生输出的像素验收；当前原生导出对 production-library 附加标记的快照兼容性另有待解决项，不能跳过构建清单检查来获得通过。
