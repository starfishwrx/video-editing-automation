# RFC — 架构与关键设计决策

本文记录已经定案、后续 agent 不应重新争论的边界。实现细节见 `docs/architecture.md` 与 `docs/subsystems/`；决策背景、状态及证据见 [ADR 索引](adr/README.md)。

## 1. 仓库形态

| 决策 | 选择 | 理由 |
|---|---|---|
| 包布局 | `packages/video-v1.x.y/` 多版本并存 | 历史包与校验清单绑定；禁止改归档 |
| 活代码目录名 | 固定 `packages/video-v1.5.3/` | 小版本升级只改 `VERSION`，避免整树改名与路径断裂 |
| 真实版本 | 只读 `VERSION` / `release-manifest.json` | 禁止在提示词或代码里手写版本号做等值闸门 |
| 生产真相 | 部署机器的 `ACTIVE_VIDEO_RUNTIME.json` + 实际安装文件 | 候选 worktree ≠ 已部署 |
| 开发入口 | 干净 worktree 跟踪 `origin/main` | 勿把带大量未提交覆盖的 fork 检出当主树 |

## 2. 端到端流水线

```
环境注入 → manifest → 通用工具链与所选写稿后端预检
  → shot_analyzer / vision_analyzer
  → ai_editor（描述召回 → 候选实际抽帧复审 → 整稿编排）
  → shot_analyzer / timing_contract（实际源窗、证据与音频分配重验）
  → OrchestrationEngine 顺序闸门
  → headless_production → 单独安装的无头后端写原生草稿
  → draft_visual_qc v5 / diagnostics
  → closed_loop / draft_delivery / NAS 分发
```

引擎闸门形态统一为 `Optional[dict]`：`None` 放行，`dict` 终止并带原因。新增闸门必须带「必拦样例」测试。

默认视觉后端为火山 Ark GLM-5.3-Flash（`glm-5.3-flash`）。运行时只保留 GLM（`volc_glm` / `glm` / `volc`）与 Qwen（`qwen` / `qwen_compatible`）直连；Codex CLI、Anthropic 和 CCSwitch 通道已移除，旧值和未知值明确拒绝。文字复核与 `JY_AI_EDITOR_QWEN` 保留。模型名称与覆盖优先级见[模型说明](model-and-vision.md)。显式 `legacy` 沿用旧 Top3、场景换镜和 QC v3；默认 AI 的单源分相可以复用 `phase_timing`，历史无角色计划可用旧词表作召回，但必须重新看图并取得完整组认证。新计划按语义角色分组，不能靠关键词裁决支持。QC v3 不能认证新模式草稿。

## 3. Formal vs Preview

- **formal**：只接受完整证据与 `PASS`；动作放不下记 `unmet`，不得用 `settled` / `lead_in` / `pool_fill` 冒充正式完整动作。
- **preview**：允许黑屏补位、证据收窄等到观察态，结果必须带未认证理由；禁止进入正式交付与表格「已完成」。

视觉等级与证据闸门不可放宽。legacy 候选按 `DIRECT → ACCEPTABLE_DEGRADED → MATERIAL_GAP`；默认 AI 模式以当前像素观察与新收据判断，`DIRECT` 表示按本句用途正常匹配，不把体验展示解释成物理实验结论。

## 4. 规则分层

- 视觉业务政策：`RULES.md` + `WINDOWS_VIDEO_CLOSED_LOOP_RULES_*.json`（版本 **1.4.1**）。
- 品类剪辑规则：`orchestrator/rules/global.json` + `weipin.json` / `zhipin.json`。
- 默认 AI 模式消费适用的全局默认值与实际证据合同；旧场景换镜/词表裁决只适用于 legacy。
- 气口、字幕默认坐标等跨品类口径进 `global`；品类包只叠加倍速、场景词、卖点识别等差异。

纸品未指定数量的囤货展示与包装同块指向合同见 [ADR 0002](adr/0002-ai-selection.md)。显式未指向、否定观察与高清复核空结果不得被描述覆盖；普通原文定位和动作对象须保持字段语义一致。

## 5. 写稿与交付边界

- 端到端写稿只走 `doubao-jianying-orchestrator`；默认 `headless_production` 使用独立授权后端，空 BGM 自动准备。`jianying-editor` 的 JyProject / vendor API 保留给显式 legacy 与兼容能力，仅无头构建明确返回旋转/HDR、像素格式或全范围不确定等媒体兼容错误时，自动以新草稿名尝试旧写稿器；其他失败不自动回退，最终验收不变。
- `env` 默认汇总生产环境，包含所选后端；`--input` 读取任务覆盖，`--profile base` 仅报告通用工具链。完整检查失败时返回非零；它不认证云端密钥。
- 恢复写稿消费 `shot_match_report.json` 中的成功结果与原始输入绑定，裸 `matched_manifest.json` 仅作建稿输入。当前匹配失败会使当前报告失效；输入变化和无绑定旧报告不能自动用于写稿。
- 任务级重试和阶段恢复分别持久记账；阶段恢复绑定原始运行及冻结的输入、音频和时长，复用已完成观察与成功阶段。输入变化、必要快照损坏或预算耗尽明确阻断，不静默回到完整 build；预检阻断不消耗付费阶段恢复预算。详见 [ADR 0009](adr/0009-frozen-input-recovery.md)。
- 禁止手改 `draft_content.json` / `draft_info.json` / `draft_meta_info.json`，禁止任务内临时 `gen_*.py`。
- 共享交付后必须校验时间轴上的**字面绝对路径**与字节；副本哈希一致不等于媒体可打开。
- 加密编辑会话禁止静默回落旧明文 sidecar；恢复生成版需用户确认 + 成功任务证据 + 新 UUID。

## 6. 依赖与发布

- 依赖合同：`pyproject.toml` + `uv.lock`；宿主与包统一 uv。
- 发版入口：`python -m tools.release_package build|verify`（见 `references/validated_fix_to_release.md`）。
- 历史手工 `PACKAGE_CONTENTS.sha256` PowerShell 流程仅适用于旧包说明；当前包走合包命令。
- 冻结：`video-v1.5.2/` 及更早只读。

## 7. 文档分层（对齐鸭哥脚手架）

| 文件 | 职责 |
|---|---|
| `docs/prd.md` | 目标用户、成功标准、非目标 |
| `docs/rfc.md` | 本文件：架构边界与定案 |
| `docs/working.md` | 按日 changelog + 指向 `LESSONS.md` |
| `docs/test.md` | 什么算验证完成 |
| `AGENTS.md` | Agent 路由表（先读） |
| `docs/subsystems/*` | 模块级实现合同 |
| 包内 `references/*` | 运行时合同、交付恢复、质量课 |

子系统文档里的代码路径一律指向 `packages/video-v1.5.3/...`；若标题仍写历史版本号，以 `VERSION` 为准。

## 8. Windows 脚本运行与迁移

- 2026-10-30：采用技能内部功能包布局，保留双技能安装组件。50 个模块迁入 9 个功能包，Clip 数据模型独立为 domain；旧路径仅兼容导入，使用同一模块对象保持缓存/锁/patch 身份。按文件加载的证据合同、规则包和独立分发器保留位置。约束和已知留存耦合见 `docs/modular-development.md`。
- 闭环以 `tools/workflow/closed_loop.py` 为单一实现，两条旧脚本统一执行Windows 自动验收与分发身份合同；worker 的纯任务合同进入 `tools/queueing/contracts.py`。整体运行由 Windows 定时脚本复用 dispatcher，不恢复历史 Agent 调度链。

- 外部调度与现有 pipeline 联接保留；升级先备份再换 ACTIVE 指针。
- 分析缓存键变更（如 `product-visual-v2`）会触发素材重分析，属预期成本。
- Mac 交接、人工 prepare/collect 和批准标记已删除。`tools/acceptance` 只验证四项核心：输入身份、运行时身份、草稿包、独立视觉 QC；历史人工批准文件不参与运行。

## 9. 纸品最终配音

最终变速、响度和一次有界连续边缘修复先于真实音频定长及选镜。纸品五帧上限、写稿前实际切片闸门、视觉证据与冻结恢复边界均保留；检测失败不能冒充零静音，内部停顿不能并入尾静音。见 [ADR 0010](adr/0010-final-narration-gate.md)。

## 10. 离线验证的执行层次

开发按模块定向检查，独立审阅收敛后对最终代码全量一次；发版在干净解压候选完成该轮，安装仅验证必要执行面。CI 两套完整 discover 并行并以稳定检查名失败关闭聚合，PR/main/手动均保留全量。详见 [ADR 0011](adr/0011-layered-offline-validation.md)。
