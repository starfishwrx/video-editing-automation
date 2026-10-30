# 纸品选镜单模块验证

这个入口使用已分析镜头重新选镜，固定原句配音时长。它只输出选镜方案和 FFmpeg 诊断预览，不运行全库识别、TTS、建稿或交付，也没有接入默认生产流水线。

## 模块边界

- `orchestrator/selection/paper_selection.py`：按已检索的候选和本句反馈重新看图，验证动作、结果和连续源窗口，比较可行方案。每句最多三个候选，准备及请求最多两路，复用现有 GLM/Qwen 适配、账户槽位和持久缓存。
- `orchestrator/selection/paper_retiming.py`：纯时间求解，不调用模型。关键动作、结果和其他保护区保持 1 倍；只有明确允许压缩的过程可加速，优先不超过 2 倍，必要时最多 3 倍。时间性声称的证据全部保护。不能用慢放、定格、跳过中间动作或改变句长补足。
- `tools/paper_selection_replay.py`：从原版逐句表读取真实句长和文案，覆盖请求中的同名字段，输出 `selection.json`。`--round` 只标记第 1–3 轮，操作者负责最多三轮定向调整，不自动循环整个项目。
- `tools/selection_preview.py`：读取草稿和可选的选镜覆盖，保留原音轨、音量、字幕与时间范围，在新目录渲染诊断 MP4。拒绝改变句长、覆盖带原声音的视频，以及短于一输出帧的分段。

## 输入与命令

先冻结业务表实际链接指向的草稿、该任务的镜头分析与配音信息。不同尝试的 `done.json` 不能替代这个原版。使用预览工具的 `video_rows()` 可取得原版逐句表，字段包括从 1 开始的 `index`、`segment_us` 和 `text`；媒体路径仍按原草稿目录解析。

请求是 JSON 列表，例如：

```json
[
  {
    "index": 7,
    "requirement": "一次完整单张抽取，保留包口没有连带抽出的结果",
    "candidate_ids": ["source_001_shot_001"],
    "candidate_windows": {
      "source_001_shot_001": {"start": 400000, "end": 1900000}
    },
    "require_action": true,
    "require_result": true
  }
]
```

窗口单位为源素材绝对微秒，必须落在这份分析的原场景内。候选编号必须来自同一份冻结分析；文件路径和当前源身份才决定实际复核对象。`candidate_windows` 只限定本次看图区域，不能直接授予语义通过或裁掉旧证据。

在活包根、已配置 GLM 或 Qwen 环境变量的进程中运行：

```powershell
uv run --frozen python -X utf8 -m tools.paper_selection_replay --manifest $manifest --analysis $analysis --baseline-table $baselineTable --requests $requests --report-dir $roundReport --round 1
uv run --frozen python -X utf8 -m tools.selection_preview --draft $baselineDraft --draft-root $originalDraftDirectory --output $newPreviewA --name A
uv run --frozen python -X utf8 -m tools.selection_preview --draft $baselineDraft --draft-root $originalDraftDirectory --output $newPreviewB --overrides $selectionJson --name B
```

输出目录必须位于草稿外且尚不存在。模型凭据只从已有环境变量读取，不写进请求或报告。私有任务输入、素材、模型回复和 MP4 留在隔离验证目录，不进入源码仓库。

## 观察和失败语义

每个实际候选窗口抽取 16 个带时间戳的帧，纸品入口保持原宽高比，避免把竖屏包装拉宽后误判厚实。新拼图参数使用新缓存键；原帧仍复用，旧形变拼图及其回复不作为新输入的观察。源文件、窗口、提示词、图片或模型路由变化都按已有身份重新复核，不触发全库重识别。

观察必须明确商品、匹配事实、证据区间和无缝相位；`key_action`、`result` 禁止加速。越界窗口、缺动作或结果、无效响应和复核中源文件变化都不选中。符合画面但时长放不下会返回 `DURATION_FIT_CONFLICT`；源窗短于句长也不靠补帧通过。坏候选单独记录，其余候选可继续比较。

只对可行方案输出覆盖。未解决句保留原选镜，并在 `unresolved` 列出；这可能保留原版已有的高倍速或缺陷，不能据此声称整片达到 3 倍上限或质量通过。退出码 0 是全部请求得到方案，退出码 2 是仍有未解决项。`SELECTION_READY` 也只是方案就绪，不是独立视觉 QC 或正式验收 PASS。

## A/B 评价

使用同一诊断渲染方式、原音轨、字幕、BGM 和逐句时间范围。先看问题片段，再完整连续播放，对照动作开始、接触、完成和可读结果；还要查新重复、跳切和音画边界。模型可能把同一动作误称为不同角度，或把下一次动作误称为静置，必须按像素纠正，不能只按 `matched` 放行。

预览保留文字、时间和基本位置，字体度量为近似；复杂特效、裁切动画等不是剪映原生渲染。比较参考工程时同时标明读取时间、媒体可用性和是否静音摘录，不能把历史快照当成当前保存版本。

本轮纸品 34 验证复用 214 条分析、13 句真实配音，三轮后得到四处原速替换，揉搓仍为时长冲突。验收结论为未通过：没有实现解决完整动作适配短口播、整片达到人工示例的目标。四处局部改善不能抵消这一失败，也不能把当前候选求解失败推广为人工剪辑方法不可行。私有 A/B、逐句表和画面审核报告另存隔离目录；没有覆写原稿、修改配音或回写业务表。代码只保留为隔离验证能力，未部署。

回归：`test_paper_retiming.py`、`test_paper_selection.py`、`test_paper_review_media.py`、`test_selection_preview.py`，以及现有 AI 选镜与架构测试；最终完整命令见 [测试策略](test.md)。
