# 0005 原生写稿、兼容回退与独立 QC

- 状态：已记录的选择；部署状态须另核验
- 原决定日期：默认无头与自动选曲的确切日期未确认；兼容回退记录于 2026-11-06。
- 补记日期：2026-11-09
- 关联／替代：除文中说明外，未发现明确的 ADR 替代关系。

## 背景与选择

默认无头原生写稿，空 BGM 自动选曲；仅无头构建返回明确的 `SOURCE_ROTATION_HDR_METADATA_UNSUPPORTED`、`SOURCE_ROTATION_PIXEL_FORMAT_UNSUPPORTED`、`SOURCE_ROTATION_FULL_RANGE_UNCERTAIN` 或 `SOURCE_ROTATION_UNSUPPORTED` 错误时，自动用新草稿名尝试旧写稿器。formal 只认完整证据和独立 QC PASS；preview 不得转成正式交付。默认 AI 独立 QC 合同为 v5。

## 原因与代价

兼顾自动化和明确的媒体兼容问题，回退本身不证明质量；代价是仍需维护旧后端并完成同样的最终验收。

## 证据

[RFC](../rfc.md)、[正式复盘](../plans/20261106-weipin-formal-retro.md)、[写稿引擎](../../packages/video-v1.5.3/doubao-jianying-orchestrator/orchestrator/application/engine.py)、[QC 合同](../../packages/video-v1.5.3/doubao-jianying-orchestrator/orchestrator/visual_qc_contract.py)。
