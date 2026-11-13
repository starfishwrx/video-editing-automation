# 活跃包入口与规则权威清理盘点

## 范围与结论

盘点基线为 main `388bae686aa165cc12915d714eeb7581e41e97de`。路径中的 `video-v1.5.3` 是活跃目录名，不能据此判为旧包；本轮不处理历史包、不改生产算法、不部署。

以下逐项覆盖该基线 Git 跟踪的活跃包入口候选：排除 `tests/`、`vendor/`，收集 Python 顶层 `if __name__ == "__main__"` 及 `.ps1`、`.bat`，共 **55 项**。配置样例也计入候选，但不是生产入口。兼容模块等无此顶层条件的入口另见末尾说明；这不是所有可导入函数或动态加载点清单。后续新增检查器不计入这一固定基线数量。

入口表“路径”列以 `packages/video-v1.5.3/` 为根；证据中的 `docs/` 默认指仓库根文档（另注明“包内”的除外）。下文 `orchestrator/` 是包内 `doubao-jianying-orchestrator/orchestrator/` 的缩写，`rules/` 位于该目录下。表中源码本身的命令解析、转发或调用是用途证据，不等同于实际使用频率。**未核实外部调用、宿主计划任务、安装副本和人工命令历史，因此本轮无脚本删除。** 未执行会启动业务队列、服务请求、安装、同步、回写或交付的命令。

## 55 项逐项分类

| # | 路径 | 类别与处置 | 保留理由、可复查证据或待查项 |
| --- | --- | --- | --- |
| 1 | `batch_worker.py` | 正式执行，保留 | worker 与 dispatcher 的队列执行职责不同；见 `docs/subsystems/package-tools.md`、`tools/queueing/contracts.py`。 |
| 2 | `build_manifest.py` | 发布工具，保留 | `tools/release_package.py` 的 `builder()` 加载该实现；不是历史生成物。 |
| 3 | `closed_loop.py` | 兼容入口，保留 | 转发 `tools/workflow/closed_loop.py`；支持旧路径和包外工作目录。基线注释中的 Mac gates 属文字漂移，不是删入口理由。 |
| 4 | `doubao-jianying-orchestrator/orchestrator/application/cli.py` | 底层建稿 CLI，保留 | `doubao-jianying-orchestrator/run.py` 进入此实现；与正式队列入口层次不同。 |
| 5 | `doubao-jianying-orchestrator/orchestrator/quality/draft_visual_qc.py` | 草稿检查，保留 | `orchestrator/module_map.json` 声明旧平面模块映射；架构检查将旧 `draft_visual_qc` 列为可执行兼容入口。 |
| 6 | `doubao-jianying-orchestrator/orchestrator/runtime/platform_env.py` | 环境诊断，保留 | `module_map.json` 映射及 `tools/check_architecture.py` 的 RUNNABLE 列表保留其 CLI 合同。 |
| 7 | `doubao-jianying-orchestrator/orchestrator/ship_distribute.py` | 交付工具，保留 | 复用 `tools.draft_delivery` 的实际引用收集与发布；见 `docs/subsystems/package-tools.md` 闭环与交付。 |
| 8 | `doubao-jianying-orchestrator/orchestrator/vision/legacy_cache_import.py` | 迁移兼容，保留 | 模块映射和架构检查显式保留可执行旧入口；名称 legacy 不等于无用途，外部迁移需求待查。 |
| 9 | `doubao-jianying-orchestrator/run.py` | CLI 启动器，保留 | 转入编排器 CLI；薄启动器不另维护生产算法。 |
| 10 | `examples/config.example.ps1` | 配置样例，保留 | 供 Windows 运行配置参考，不应作为执行任务入口；不是可删除的旧生产配置。 |
| 11 | `jianying-editor/scripts/cloud_manager.py` | 编辑器底层工具，保留 | 有独立命令解析；属于编辑器能力层，不因上层有 worker 就视作重复。外部直接调用未核实。 |
| 12 | `jianying-editor/scripts/draft_inspector.py` | 草稿诊断，保留、补导航候选 | 提供 list/show 诊断，受 `release-manifest.json` 跟踪；准确文件名引用较弱，仍不足以认定废弃。 |
| 13 | `jianying-editor/scripts/jy_wrapper.py` | 编辑器适配 CLI，保留 | 编辑器操作入口，与上层流程编排职责不同；见 `docs/subsystems/jianying-editor.md`。 |
| 14 | `jianying-editor/scripts/universal_tts.py` | 配音工具，保留 | 有独立 TTS 命令入口；配音能力不是队列调度的替代实现，见 `docs/subsystems/audio-voice.md`。 |
| 15 | `jy_poll.ps1` | 退役提示桩，保留 | 仅提示 `LEGACY_RUNNER_RETIRED` 并非零退出；防止旧调用静默跑旧流程，见 package-tools 文档。 |
| 16 | `nas-tools/fix_local.ps1` | NAS 本地化适配，保留 | 调用同目录发布副本 `nas_portable.py localize`；由本地化 BAT 使用，不能只按源码目录没有该副本判断损坏。 |
| 17 | `nas-tools/nas-tool.ps1` | NAS 导入导出，保留 | Import/Export/Inspect 参数合同；取稿、上传 BAT 和 `sync.ps1` 调用。 |
| 18 | `nas-tools/sync.ps1` | 已安装任务兼容，保留 | 读取已配置目录并调用 `nas-tool.ps1 -Action Import`；本脚本不安装任务，宿主调用待核实。 |
| 19 | `nas-tools/一键取稿.bat` | 人工取稿，保留 | 显式调用 `nas-tool.ps1 -Action Import`，支持拖入目录。 |
| 20 | `nas-tools/上传草稿.bat` | 人工上传，保留 | 显式调用 `nas-tool.ps1 -Action Export`，与取稿方向不同。 |
| 21 | `nas-tools/安装草稿自动同步.bat` | 显式安装，保留、不执行 | 复制工具并注册 `JianyingDraftSync` 计划任务；涉及宿主状态，不能与只读诊断混用。 |
| 22 | `nas-tools/素材本地化修复.bat` | 人工修复，保留 | 调用 `fix_local.ps1`，面向用户双击/拖拽流程，不是重复实现。 |
| 23 | `run_task.template.ps1` | 退役提示桩，保留 | 与 JyPoll 一样 fail-fast，旧 JyRun 调用可获得明确迁移提示；不是活跃任务模板。 |
| 24 | `scripts/install_pipeline_task.ps1` | 显式安装，保留、不执行 | Windows 正式流水线的计划任务安装入口，见 `docs/windows-pipeline.md`。 |
| 25 | `scripts/run_pipeline.ps1` | 正式 Windows 入口，保留 | 单条、批量和定时执行使用同一入口，调用 `tools.workflow.scheduled`；见 Windows 流水线文档。 |
| 26 | `scripts/uv_sync.ps1` | 依赖环境准备，保留 | 与任务执行分离的依赖同步脚本；外部安装流程未核实，不删除或自动执行。 |
| 27 | `tools/ab_stability_single_pass.py` | 性能 A/B，保留、补导航候选 | 导入 `bench_gpu_decode` 辅助函数，对比 `JY_STABILITY_SINGLE_PASS` 两种模式；生产 `media/stability.py` 和对应测试仍支持此开关。 |
| 28 | `tools/acceptance/evaluation.py` | 验收实现，保留 | 当前四项证据验收；旧 `tools/production_eval.py` 转发此实现，不是两套验收。 |
| 29 | `tools/batch_dispatch.py` | 正式批次/恢复，保留 | run/resume 命令，阶段恢复与账户准入；见 package-tools 文档。 |
| 30 | `tools/bench_gpu_decode.py` | 解码基准，保留、补导航候选 | 被 A/B 工具导入，且在发布清单中；生产直接引用少不证明性能工具废弃。 |
| 31 | `tools/check_architecture.py` | 离线守卫，保留 | AST 导入边界、兼容 wrapper 和模块映射检查；用于阻止结构退化。 |
| 32 | `tools/classify_feedback.py` | 反馈归类，保留 | 输出可审计规则候选；与反馈事件存储不同，见 package-tools 文档。 |
| 33 | `tools/closed_loop.py` | 兼容入口，保留 | 转发唯一的 `tools/workflow/closed_loop.py`，共享模块状态；不是第二套闸门。 |
| 34 | `tools/delivery_runtime.py` | 只读安装诊断，保留 | `PIPELINE_FILES` 同时覆盖 pipeline 与 skills 运行位置；不负责安装或部署。 |
| 35 | `tools/display_selection.py` | 显式选镜预览，保留 | 固定口播、指定句和候选窗口，调用 `application.display_optimization`；不替代默认队列。 |
| 36 | `tools/draft_delivery.py` | 交付共用实现，保留 | `collect_delivery_stage`、`publish_tree` 被闭环和分发复用；避免再复制交付规则。 |
| 37 | `tools/eval_bootstrap.py` | 评估输入冻结，保留 | 独立冻结输入；与最终验收判定不同，见 package-tools 文档。 |
| 38 | `tools/extract_voice_catalog.py` | 人工目录快照，保留 | 模块说明及检查限制为选定本地缓存的只读提取，不改运行时 catalog。 |
| 39 | `tools/feedback_candidates.py` | 私有反馈事件，保留 | 追加候选与审核/回归/发布事件，不在运行中提升全局规则；见 package-tools 文档。 |
| 40 | `tools/headless_review_delivery.py` | 审稿副本交付，保留 | 复用 `publish_tree`，明确不调用正式 finalizer、不标业务完成；区别于正式交付。 |
| 41 | `tools/headless_runtime.py` | 只读后端预检，保留 | `check` 调用 `orchestrator.runtime.headless.preflight()`，不启动业务任务。 |
| 42 | `tools/material_library.py` | 显式素材库工具，保留 | ingest/publish/review/archive 与索引职责不同，导入 material_index；不在默认生产路径自动写库。 |
| 43 | `tools/nas_portable.py` | 可移植包实现，保留 | 被发布工具导入并用于 NAS 工具交付；源码与部署复制位置需区分。 |
| 44 | `tools/paper_selection_replay.py` | 固定输入选镜 A/B，保留 | 复用已有分析、原音轨和句长，不执行生产建稿/交付；见 `docs/paper-selection-replay.md`。 |
| 45 | `tools/production.py` | 显式固定选镜制作，保留 | prepare/preview/render 等 CLI，不启动业务队列；与默认生产入口不同使用合同。 |
| 46 | `tools/production_demo.py` | 合成演示，保留 | 调用 `tools.production`，生成可重现合成输入；模块说明排除模型、TTS、草稿和业务访问。 |
| 47 | `tools/production_eval.py` | 验收兼容入口，保留 | 转入 `tools/acceptance/evaluation.py`；已有导入与运行副本完整性合同需要保留。 |
| 48 | `tools/purpose_production.py` | 显式任务定向实验，保留 | inspect/select/render/review/repair 调用 `application.purpose_production`，不写队列、NAS 或业务表。 |
| 49 | `tools/release_package.py` | 源码发布构建/校验，保留 | 读取版本与 manifest，使用 `build_manifest.py`；模块说明明确不安装、不发布到运行环境。 |
| 50 | `tools/selection_preview.py` | 隔离预览，保留 | 与 paper replay 配套，固定输入对照而非正式交付；见 paper-selection-replay 文档。 |
| 51 | `tools/sentence_review.py` | 私有句级审稿回放，保留 | collect 冻结输入并调用实际 matcher 离线比较；人工标签不授予正式交付许可，见包内 `docs/sentence-review.md`。 |
| 52 | `tools/throughput_calibration.py` | 批次证据分析，保留 | 比较真实运行收据，不额外调用模型或清缓存；见 package-tools 文档。 |
| 53 | `tools/weipin_experiment.py` | 显式实验验证，保留 | roles/observe/pair/motion 等阶段命令；无业务回写，见 `docs/weipin-experiments.md`，不能因部分能力进入默认生产就整文件删除。 |
| 54 | `tools/workflow/closed_loop.py` | 闭环唯一实现，保留 | audit/archive-parent/table-update/failure-update 等合同；两个旧入口只转发。 |
| 55 | `tools/workflow/scheduled.py` | 队列轮次编排，保留 | Windows 主脚本调用，负责重试与轮次记录；不是另一个宿主安装脚本。 |

### 不应误判的“重复”

- `orchestrator/module_map.json` 与 `tools/check_architecture.py` 定义平面旧模块到分层实现的兼容关系。生成/转发式旧模块可能没有顶层 `__main__` 条件，所以不包含在上述 55 项中；不能据此删除。
- `tools/material_index.py` 不在上述入口识别集合，但被 `material_library.py` 使用；索引与库操作不是两套同义实现。
- `draft_inspector.py`、两个性能工具可作为“补导航、核实调用”的候选，不是“待删名单”。删除前还要查实际安装、宿主命令/计划任务、发布消费者和回归覆盖。

## 规则权威与重复说明的处理

这里列的是本次核对的规则域及维护边界，不宣称已证明所有自然语言规范完全一致。文档摘要、运行时校验、测试证据职责不同，不因描述相近就删除。

| 规则域 | 权威及执行位置 | 其他说明的处置 |
| --- | --- | --- |
| AI 编辑与独立视觉 QC | 包内 `docs/AI_EDITOR.md`；`orchestrator/ai_editor_contract.py`、`visual_qc_contract.py` | `RULES.md` 做导航，不再复制易漂移的 QC 版本号。基线 `RULES.md` 写 v4，而 AI_EDITOR 写 `chatcut-visual-qc-response-v5` 并要求旧 v4 重审，这是明确文字冲突。 |
| 生产播放策略 | `orchestrator/policy/playback.py`；根 `docs/production-playback.md` 解释策略与证据 | 其他文档引用该策略，不另建数字阈值来源；ADR 保留决策背景。 |
| 品类规则 | `orchestrator/rulepack.py` 合并 `rules/global.json` 与品类 JSON | `RULES.md` 指向纸品 `zhipin.json`；摘要不替代运行规则，不把反馈候选直接提升为全局规则。 |
| 字幕参数与执行 | `orchestrator/policy/production_parameters.py`、`rules/zhipin.json` 及编辑器 `text_ops` | `docs/subtitle-layout.md` 解释布局合同；SKILL 的调用约束不是可整份删除的重复规则。 |
| 正式交付与自动验收 | 包根 `WINDOWS_VIDEO_CLOSED_LOOP_RULES_20260930.json`、`tools/workflow/closed_loop.py`、`tools/acceptance/evaluation.py` | 日期文件仍是 closed_loop 的 DEFAULT_POLICY，evaluation 仍检查它，不能按旧日期归档。兼容入口注释要指向当前 Windows 验收。 |
| 架构与包范围 | `tools/check_architecture.py`；活包 `tools/check_repository_scope.py`（PR #74，检查仓库变更范围） | 文档说明如何运行；可执行边界优先，不扩张 AGENTS 为另一份实现。 |
| 历史决定与验证 | `LESSONS.md`、`docs/working.md`、CHANGELOG、ADR | 保留提交/时间背景，不能当成当前测试或生产验收。历史验证归档已由 PR #75 处理，本清单不复制其记录。 |

## 与已有清理工作的关系

- PR #73（`docs/repository-entrypoints`）整理仓库导航与入口文档；本表补充逐项用途，不再复制全套导航。
- PR #74（`fix/repository-scope-guard`）实现仓库包范围守卫；这里不重复实现或改变历史包边界。
- PR #75（`docs/archive-historical-validation`）整理历史验证记录；本表只记固定基线的静态证据，不更新或搬迁旧测试结果。
- 可安全先修的是已证实的说明漂移：`RULES.md` 的 QC v4 表述，以及根 `closed_loop.py` 注释中的 Mac gates。修正文案不意味着删除兼容适配器、修改验收政策或宣布真实生产通过。

## 验证边界

本清单通过 Git 跟踪文件、AST 入口条件、脚本调用和相关文档交叉检查。完整入口集合可用 `git ls-files -z`（正确处理中文 BAT 文件名）配合 Python AST 重建。没有用 grep 零命中代替依赖证明。未核实外部调用，因此全部保留；未执行生产流程，因此不构成业务效果、Windows 运行环境或实际成片验收结论。

## 动态加载适配清单

本续批另扫描活包非测试/非vendor Python，列出 **16 个**动态适配文件（包含3个 `exec` 兼容入口），见[机器可检查清单](../packages/video-v1.5.3/tools/dynamic_loading_adapters.json)。每项保留用途、已有验证入口及API数量。执行 `python -m tools.check_dynamic_loading` 或现有架构检查可检测清单漂移；本次没有移动或删除这些运行时代码。

清单是增删位置/API数量的检查，不证明加载目标、函数间移动或反射/赋值别名安全。限制与补充决策见[模块化开发](modular-development.md#动态加载适配器盘点2026-11-12)和[ADR 0003](adr/0003-windows-modules.md)。当前脚本清单已完成固定范围盘点；外部实际使用核验未完成，未来删除候选仍须另核实。规则域归属已整理，但并非全仓自然语言规则的形式等价证明。
