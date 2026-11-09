# 0005 原生写稿、兼容回退与独立 QC

- 状态：已采纳（回溯补记）；基线已有相关实现，部署状态须另核验
- 原决定日期：默认无头与自动选曲的确切日期未确认；兼容回退记录于 2026-11-06。
- 补记日期：2026-11-09
- 关联／替代：关联：[AI 选镜](0002-ai-selection.md)、[实验组](0007-experiment-groups.md)、[独立时间线](0008-timeline-preview.md)、[冻结输入与阶段恢复](0009-frozen-input-recovery.md)。除文中说明外，未发现明确的 ADR 替代关系。

## 背景与选择

默认无头原生写稿，空 BGM 自动选曲；仅无头构建返回明确的 `SOURCE_ROTATION_HDR_METADATA_UNSUPPORTED`、`SOURCE_ROTATION_PIXEL_FORMAT_UNSUPPORTED`、`SOURCE_ROTATION_FULL_RANGE_UNCERTAIN` 或 `SOURCE_ROTATION_UNSUPPORTED` 错误时，自动用新草稿名尝试旧写稿器。formal 只认完整证据和独立 QC PASS；preview 不得转成正式交付。默认 AI 独立 QC 合同为 v5。

无头后端是单独授权和安装的依赖；按任务选择的后端、字体、媒体工具和配置在付费步骤前预检，基础工具链缓存不能替代后端即时检查。源码包不携带官方 DLL、私有配置或业务缓存；环境预检不认证云端密钥或成片质量。

逐句 QC 拒镜累计保存并绑定当前文案、编辑输入和源文件身份；恢复不因最新报告覆盖而遗忘早期拒绝，输入或来源变化后不继承旧拒绝约束，也不清空有效像素／TTS 观察缓存。拒绝名单不替代独立 QC。实验组的定向修复重新核验整组关系，锁定已接受邻句的源窗、速度和时长及组外结果；只有失败句可更换画面，最终仍复核当前草稿。

正式分发前核对当前输入、运行时身份、草稿包和独立视觉 QC 四项 PASS。媒体复制在同级隐藏目录核验实际路径和字节后发布；归档须将每版媒体重新绑定最终目录，复制哈希一致不能代替可打开路径。旧加密编辑会话不能静默回落旧明文 sidecar；恢复原始生成版必须取得不保留后续编辑的明确确认，核对成功任务及生成快照，并使用新 UUID，不能声称无损恢复编辑状态。

## 原因与代价

兼顾自动化和明确的媒体兼容问题，拒绝继承与输入绑定限制重复误选和旧证据误用；交付读回与编辑恢复保护避免复制成功却打不开或覆盖后续编辑。代价是维护外置后端及有限旧后端兼容、持久拒绝状态和额外交付副本，并始终完成同样的最终验收。回退、复制和恢复本身不证明视觉质量。

## 证据

[RFC](../rfc.md)、[正式复盘](../plans/20261106-weipin-formal-retro.md)、[写稿引擎](../../packages/video-v1.5.3/doubao-jianying-orchestrator/orchestrator/application/engine.py)、[QC 合同](../../packages/video-v1.5.3/doubao-jianying-orchestrator/orchestrator/visual_qc_contract.py)、[拒镜及交付合同](../subsystems/package-tools.md)、[实验组修复](../production-playback.md)、[交付与生成版恢复](../../packages/video-v1.5.3/doubao-jianying-orchestrator/references/draft_delivery_and_recovery.md)。新增边界是对现有合同的补记，未确认的历史日期不补造。
