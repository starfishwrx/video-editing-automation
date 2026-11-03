# 子系统：视觉匹配

实际选镜位于 `packages/video-v1.5.3/doubao-jianying-orchestrator/orchestrator/selection/`，观察在 `vision/`，独立 QC 在 `quality/`，公共合同保留原位置。默认为 `ai_editor`；`manifest.selection_mode` 优先于 `JY_SELECTION_MODE`，`rule_then_ai` 为规则初排/AI 复核，显式 `legacy` 保留旧算法。模型与调用配置见[模型说明](../model-and-vision.md)。

## 1. 两条路线的共同入口

- `shot_analyzer.analyze_manifest`：只对当前任务素材场景切分、抽帧并生成候选 JSON，不替模型填写描述。
- `vision_analyzer.enrich_with_vision`：通过有标记的拼图理解描述、动作、结果与证据窗口；默认火山 Ark **GLM-5.3-Flash**（`glm-5.3-flash`）直连。
- 调用失败、无可读图片或观察缺失保持待分析/未认证，不使用猜测标签。
- 素材理解缓存可跨任务复用；缓存身份包含素材/源窗/分析规则和所选后端等，复用不等于写入持久素材库。

旧任务清单未写商品时，可由操作者使用 `python -m orchestrator.legacy_cache_import` 显式导入：必填 `--prior-task`、`--prior-manifest`、`--current-manifest`、`--prior-analysis` 和 `--cache`（路径均为 JSON）；运行环境必须使用拟复用的 transport、模型和抽帧设置。工具要求旧任务与当前商品一致、旧任务和清单的 record 一致、素材在两份清单中，且旧键能由原清单及去掉商品字段的当前清单分别重算。源文件 stat、窗口、规则、模型和采样协议任何不一致都不会导入；已有当前键不覆盖。此操作只恢复视觉观察，未复核的文字仍需补验，选镜像素复核与独立 QC 不省略。导入影响新启动的分析，不会刷新已运行进程的内存快照。导入命令不再提供可配置的 profile；观察和相位键中的历史兼容字段固定为 `profile="volc"`，身份相符的缓存可继续复用。

## 2. 默认 AI 选镜与整稿编排

调用链为 `cli._temporary_match → ai_editor.prepare → shot_analyzer.match_manifest`。

- `ai_editor._inventory` 通读脚本，根据已有描述分类和宽召回；描述只作索引。
- 卖点候选每轮 8 个，逐个重新抽带时间戳的 8 帧供模型观察，不明确时加密到 16 帧。未审完或存在未知项时，不宣布全库缺素材。
- 先给具体卖点安排画面，再给普通介绍/钩子安排同商品展示；含卖点的钩子仍按卖点检查。一句至少有一个具体卖点得到实际画面支持即可，其他项写入 `uncovered_selling_points`，不声称已经证明。
- 体验表达允许贴切展示；明确功能结果、数量或数值比较必须有相应画面。包装文字只支持该标称，不能替代功能演示。
- AI 统一编排单镜/多镜；程序核验音频分配、素材内容、源窗、证据覆盖和重复区间。失败只修当前句，其他句保持；最多三轮编排。源窗超出旧证据时重新观察提议的小窗口，每句最多三个不同窗口，不能裁剪旧证据冒充新观察。
- 正常匹配按音频求解速度，沿用最高 20 倍边界并尽量接近原速；关键动作和结果仍须完整覆盖。
- 新模式不使用旧词表、A/B 强度或旧场景换镜器重新裁决语义。未知、漏段、失败及输入变化都不能获得正式 PASS。

主要产物是 `ai_editor/plan.json`、逐段 `ai_selection` 证据、`matched_manifest.json` 和引擎保存的 `ai_selection_identity.json`。完整字段、缓存与扩展策略见[包内 AI 选镜合同](../../packages/video-v1.5.3/docs/AI_EDITOR.md)。

## 3. 独立后验 QC

`draft_visual_qc.py` 读取当前草稿，按最终使用的源窗口重新抽取开始、过程、结束及关键结果帧。它默认复用同一个视觉后端，但不接收选镜分数、理由或通过结论。

- AI 模式：公共合同 `visual_qc_contract.py` 定义 `chatcut-visual-qc-response-v5` / `chatcut-visual-qc-v5`，独立 QC 使用同一句至少一个具体卖点的口径。`DIRECT` 表示按本句用途正常匹配，不将体验展示解释成物理证明；v4 旧报告须重新复核。
- legacy：`visual_qc_contract.py` 保留 v3 合同；v3 不认证 AI 模式的新草稿。
- worker/Eval 重读报告，并核对当前草稿与源素材身份。缺字段、非法置信、漏段、失败或旧报告不自动升级为 PASS。
- 混用模式、换镜后沿用旧收据、源内容变化都需阻断或重新验证。正式交付还需人工审阅。

## 4. legacy 专属策略

以下内容用于显式回滚，不能当成默认 AI 模式的执行说明：

- `vision_analyzer.match_sentences` 在已观察描述上补充句镜语义匹配，通过已配置的 GLM 或 Qwen 直连接口执行。
- Top3/Top1、`DIRECT → ACCEPTABLE_DEGRADED → MATERIAL_GAP`、窗口求解、多镜组合、复用/相邻同源/机位限制、`scene_rules` / `phase_timing`。
- 包装直证需可读标称与明确指向，不能把包装印字当功能已验证；CTA 囤货量感与包内规格分别判断。
- 单镜/多镜共用 `timing_contract` 求解；角色速度约束与证据覆盖同步传递。多样性只在同等质量候选间打散。
- 旧 preview 的黑屏/补镜与未解窗口始终未认证；旧 `demo_actions` 的 `ungated` 覆盖率不能当作已通过。

`ai_editor`、`rule_then_ai` 与 `legacy` 均支持 GLM（`volc_glm` / `glm` / `volc`）或 Qwen（`qwen` / `qwen_compatible`）直连。Codex CLI、Anthropic 和 CCSwitch 已从运行时移除；旧 transport 和未知值明确拒绝，选择 legacy 不会恢复旧服务调用。文字复核和 `JY_AI_EDITOR_QWEN` 保持可用。

## 5. 验证

纸品单模块改镜与局部变速验证使用独立的 `selection/paper_selection.py` 和纯求解器 `selection/paper_retiming.py`，从已有分析重新复核源窗口，固定配音句长；没有接入默认建稿/交付。命令、2/3 倍上限、缓存变化和未解决状态见 [纸品选镜单模块验证](../paper-selection-replay.md)。

后续动作定位用 `selection/paper_action_events.py` 记录不带目标句长的源帧事实，`selection/paper_action_windows.py` 进行帧域窗口搜索，`media/action_frames.py` 提供PTS账本和非语义运动线索。显式选帧预览及限制见 [动作窗口验证](../paper-action-windows.md)。

- `test_shared_product_vision_cache.py`：跨任务及镜头编号复用视觉和文字、同产品冷启动防重复、不同产品仍并行、旧缓存导入、否定观察复用但不放行、源文件/SKU 变更与失败不误用缓存。
- `test_vision_stage_concurrency.py`：媒体准备与 API 阶段分离、2 路准备后 20 路请求、慢请求期间持久化快结果、文字双读及 API 上限、失败拼图和跨批响应阻断（离线）。
- `test_visual_qc_response_contract.py`：QC 独立并发、乱序返回仍按原句对应、失败批次不放行。
- `test_volc_glm_vision.py`：默认模型、图片入参、直连与失败合同（模拟网络）。
- `test_ai_editor.py` / `test_ai_editor_qc.py`：召回、看图观察、整稿编排、窗口和独立盲审合同。
- `test_selection_recovery.py`：品类不适用标签记录、实际小窗复核、只修失败句、观察缓存续用与服务商错误归因。
- `test_visual_matching_governance.py` 等：legacy 的证据、复用、机位、缓存与预览规则。
- 默认完整回归命令见 [`../test.md`](../test.md)。真实模型效果与人工成片验收单独留证。

