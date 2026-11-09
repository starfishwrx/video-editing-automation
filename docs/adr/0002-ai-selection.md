# 0002 AI 选镜与直连视觉

- 状态：已采纳（回溯补记）；基线已有相关实现，部署状态须另核验
- 原决定日期：未能从现存记录确定。
- 补记日期：2026-11-09
- 关联／替代：关联：[写稿与 QC](0005-writing-qc.md)、[短窗放置](0006-short-window.md)。除文中说明外，未发现明确的 ADR 替代关系。

## 背景与选择

默认 AI 编辑先召回，再复看候选实际画面并编排；运行时视觉及文字服务只走 GLM 或 Qwen 直连。旧策略须显式选择，旧调用通道与未知值拒绝。

一句有多个卖点时，候选实际支持其中至少一个具体点即可匹配，并保留实际支持与未覆盖的范围。不能把部分支持写成整句功效已证实，也不能用同商品、描述相似或模型置信度代替画面证据；独立 QC 使用相同的至少一个具体点口径，仍单独检查实际输出。

## 原因与代价

描述不能代替画面证据；按至少一个具体点保留有信息价值的展示，不强求每个镜头同时证明整句所有主张。代价是必须记录覆盖范围，并承担视觉复核和模型调用成本。现存记录未完整列出当时的替代方案。

## 证据

[RFC 流水线](../rfc.md)、[模型与视觉](../model-and-vision.md)、[卖点信息价值](../shot-selling-point-value.md)、[匹配合同](../../packages/video-v1.5.3/doubao-jianying-orchestrator/orchestrator/ai_editor_contract.py)、[独立 QC](../../packages/video-v1.5.3/doubao-jianying-orchestrator/orchestrator/quality/ai_editor_qc.py)。
