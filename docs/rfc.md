# RFC — 架构与关键设计决策

本文记录已经定案、后续 agent 不应重新争论的边界。实现细节见 `docs/architecture.md` 与 `docs/subsystems/`。

## 1. 仓库形态

| 决策 | 选择 | 理由 |
|---|---|---|
| 包布局 | `packages/video-v1.x.y/` 多版本并存 | 历史包与校验清单绑定；禁止改归档 |
| 活代码目录名 | 固定 `packages/video-v1.5.3/` | 小版本升级只改 `VERSION`，避免整树改名与路径断裂 |
| 真实版本 | 只读 `VERSION` / `release-manifest.json` | 禁止在提示词或代码里手写版本号做等值闸门 |
| 生产真相 | `D:/jy_pipeline/ACTIVE_VIDEO_RUNTIME.json` + 已安装技能哈希 | 候选 worktree ≠ 已部署 |
| 开发入口 | 干净 worktree 跟踪 `origin/main` | 勿把带大量未提交覆盖的 fork 检出当主树 |

## 2. 端到端流水线

```
环境注入 → manifest
  → shot_analyzer / vision_analyzer
  → visual_match（Top3 + 窗口求解）
  → scene_rules / phase_timing（品类规则包）
  → OrchestrationEngine 顺序闸门
  → jianying-editor 写草稿
  → draft_visual_qc / diagnostics
  → closed_loop / draft_delivery / NAS 分发
```

引擎闸门形态统一为 `Optional[dict]`：`None` 放行，`dict` 终止并带原因。新增闸门必须带「必拦样例」测试。

## 3. Formal vs Preview

- **formal**：只接受完整证据与 `PASS`；动作放不下记 `unmet`，不得用 `settled` / `lead_in` / `pool_fill` 冒充正式完整动作。
- **preview**：允许黑屏补位、证据收窄等到观察态，结果必须带未认证理由；禁止进入正式交付与表格「已完成」。

视觉等级顺序不可放宽：`DIRECT → ACCEPTABLE_DEGRADED → MATERIAL_GAP`。

## 4. 规则分层

- 视觉业务政策：`RULES.md` + `WINDOWS_VIDEO_CLOSED_LOOP_RULES_*.json`（版本 **1.4.1**）。
- 品类剪辑规则：`orchestrator/rules/global.json` + `weipin.json` / `zhipin.json`。
- 气口、字幕默认坐标等跨品类口径进 `global`；品类包只叠加倍速、场景词、卖点识别等差异。

## 5. 写稿与交付边界

- 端到端写稿只走 `doubao-jianying-orchestrator`；`jianying-editor` 提供 JyProject / vendor API，不是 agent 手写 JSON 的后备通道。
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

## 8. 迁移与兼容

- 外部 cron / `D:/jy_pipeline` 联接保留；升级先备份再换 ACTIVE 指针。
- 分析缓存键变更（如 `product-visual-v2`）会触发素材重分析，属预期成本。
- Mac 审阅路径与 Windows 分发路径指向同一共享文件的不同 OS 视图；暂存成功仍是 `AWAITING_MAC_REVIEW`，不是验收通过。
