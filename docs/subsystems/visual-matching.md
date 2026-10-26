# 子系统：视觉匹配

代码位于 `packages/video-v1.5.3/doubao-jianying-orchestrator/orchestrator/`。默认为 `ai_editor`；`manifest.selection_mode` 优先于 `JY_SELECTION_MODE`，显式 `legacy` 才运行旧路线。模型与调用配置见[模型说明](../model-and-vision.md)。

## 1. 两条路线的共同入口

- `shot_analyzer.analyze_manifest`：只对当前任务素材场景切分、抽帧并生成候选 JSON，不替模型填写描述。
- `vision_analyzer.enrich_with_vision`：通过有标记的拼图理解描述、动作、结果与证据窗口；默认火山 Ark **GLM-5.3-Flash**（`glm-5.3-flash`）直连。
- 调用失败、无可读图片或观察缺失保持待分析/未认证，不使用猜测标签。
- 素材理解缓存可跨任务复用；缓存身份包含素材/源窗/分析规则和所选后端等，复用不等于写入持久素材库。

## 2. 默认 AI 选镜与整稿编排

调用链为 `cli._temporary_match → ai_editor.prepare → shot_analyzer.match_manifest`。

- `ai_editor._inventory` 通读脚本，根据已有描述分类和宽召回；描述只作索引。
- 卖点候选每轮 8 个，逐个重新抽带时间戳的 8 帧供模型观察，不明确时加密到 16 帧。未审完或存在未知项时，不宣布全库缺素材。
- 先给具体卖点安排画面，再给普通介绍/钩子安排同商品展示；含卖点的钩子仍按卖点检查。一句多个卖点必须全部覆盖。
- 体验表达允许贴切展示；明确功能结果、数量或数值比较必须有相应画面。包装文字只支持该标称，不能替代功能演示。
- AI 统一编排单镜/多镜；程序核验音频分配、素材内容、源窗、证据覆盖和重复区间，必要时交回修订一次。
- 正常匹配按音频求解速度，沿用最高 20 倍边界并尽量接近原速；关键动作和结果仍须完整覆盖。
- 新模式不使用旧词表、A/B 强度或旧场景换镜器重新裁决语义。未知、漏段、失败及输入变化都不能获得正式 PASS。

主要产物是 `ai_editor/plan.json`、逐段 `ai_selection` 证据、`matched_manifest.json` 和引擎保存的 `ai_selection_identity.json`。完整字段、缓存与扩展策略见[包内 AI 选镜合同](../../packages/video-v1.5.3/docs/AI_EDITOR.md)。

## 3. 独立后验 QC

`draft_visual_qc.py` 读取当前草稿，按最终使用的源窗口重新抽取开始、过程、结束及关键结果帧。它默认复用同一个视觉后端，但不接收选镜分数、理由或通过结论。

- AI 模式：`ai_editor_qc.py` 定义 `chatcut-visual-qc-response-v4` / `chatcut-visual-qc-v4`。`DIRECT` 表示按本句用途正常匹配，不将体验展示解释成物理证明。
- legacy：`visual_qc_contract.py` 保留 v3 合同；v3 不认证 AI 模式的新草稿。
- worker/Eval 重读报告，并核对当前草稿与源素材身份。缺字段、非法置信、漏段、失败或旧报告不自动升级为 PASS。
- 混用模式、换镜后沿用旧收据、源内容变化都需阻断或重新验证。正式交付还需人工审阅。

## 4. legacy 专属策略

以下内容用于显式回滚，不能当成默认 AI 模式的执行说明：

- `vision_analyzer.match_with_codex` 在已观察描述上补充句镜语义匹配；函数名保留历史兼容，实际 transport 决定调用方式。
- Top3/Top1、`DIRECT → ACCEPTABLE_DEGRADED → MATERIAL_GAP`、窗口求解、多镜组合、复用/相邻同源/机位限制、`scene_rules` / `phase_timing`。
- 包装直证需可读标称与明确指向，不能把包装印字当功能已验证；CTA 囤货量感与包内规格分别判断。
- 单镜/多镜共用 `timing_contract` 求解；角色速度约束与证据覆盖同步传递。多样性只在同等质量候选间打散。
- 旧 preview 的黑屏/补镜与未解窗口始终未认证；旧 `demo_actions` 的 `ungated` 覆盖率不能当作已通过。

`ai_editor` 要求直连 `volc_glm`；使用旧 `codex` / `anthropic` transport 时同时显式选择 `legacy` 并验证相应后端。

## 5. 验证

- `test_vision_stage_concurrency.py`：媒体准备与 API 阶段分离、2 路准备后 20 路请求、慢请求期间持久化快结果、文字双读及 API 上限、失败拼图和跨批响应阻断（离线）。
- `test_visual_qc_response_contract.py`：QC 独立并发、乱序返回仍按原句对应、失败批次不放行。
- `test_volc_glm_vision.py`：默认模型、图片入参、直连与失败合同（模拟网络）。
- `test_ai_editor.py` / `test_ai_editor_qc.py`：召回、看图观察、整稿编排、窗口和独立盲审合同。
- `test_visual_matching_governance.py` 等：legacy 的证据、复用、机位、缓存与预览规则。
- 默认完整回归命令见 [`../test.md`](../test.md)。真实模型效果与人工成片验收单独留证。

