# 子系统：队列与闭环工具（package-tools）

- 活跃代码：`packages/video-v1.5.3/{batch_worker.py, closed_loop.py, tools/*.py, tools/workflow/, tools/queueing/, tools/acceptance/, scripts/}`；两份历史 PowerShell 入口仅为退役提示。

## 1. Windows 整体脚本与队列

- 手动或定时执行整个项目：`scripts/run_pipeline.ps1`；`tools/workflow/scheduled.py` 读取本地队列，限制每条任务的重试并保存每轮记录。安装定时任务使用 `scripts/install_pipeline_task.ps1`，不运行安装脚本就不会注册。完整参数见 [Windows 脚本运行](../windows-pipeline.md)。
- 内部批次执行入口：`python -m tools.batch_dispatch run --queue <queue.json>`；其他启动器向 `submit` 提交，制作进程须经账户级准入。同产品先完成公共准备，等待缓存不占制作池；已有草稿 QC 优先，其次就绪制作，再准备新产品。同级 FIFO。
- 按阶段恢复：`python -m tools.batch_dispatch resume --queue <queue.json> --record-id <id> --stage qc|build|material`。QC 只补未完成批次，不重做 TTS、选镜或建稿；独立 PASS 后才进入既有交付闸门。
- 每轮清空 `batch_dispatch_progress.jsonl`；完整观察保留在 `dispatch_runs/`。用 `python -m tools.throughput_calibration --run <run.json>` 比较真实批次，不清生产缓存或额外调用模型。只有已确认账户容量才使用 `--confirmed-account-capacity 40` / `80`。
- 工作根：`work/batch_queue.json`（记录列表）；每条任务落 `work/<record_id>/`
- `done.json` 为权威终态收据；历史 start 事件/活跃进程都会被它覆盖
- 环境变量控制：
  - `JY_TASK_SKIP_CURRENT=1`：跳过当前卡住的条目并选下一条
  - `JY_TASK_RECORD_ID`：仅执行指定记录
  - `JY_TASK_FORCE_REBUILD`：强制重建（谨慎使用）
- 关键文件：`progress.jsonl`（事件流）、`task_status_snapshot.json`（当前权威状态）
- 失败分类（`classify_build_failure`）：区分确定性/瞬时/提供商/环境，供上层回退与通知

## 2. 闭环与交付（`closed_loop.py` 与 tools）

- 当前候选的 `ship_one` 与修复入口复用 `tools.draft_delivery.collect_delivery_stage` 收集实际引用；`publish_tree` 在同级隐藏目录读回核验后才重命名发布。两个归档入口通过官方导出/导入将每版素材绑定到最终目录，旧目录移动不影响归档稿。安装完整性用 `tools.delivery_runtime` 同时检查 pipeline 与 skills 执行位置；见当前包 `docs/NAS_DELIVERY_STABILITY.md`。
- `audit`：读取 `task.json/done.json` 与 `report/draft_visual_qc.json`，按策略校验是否满足正式交付（明确 formal、独立 QC=PASS、四项自动验收、包完整）
- `archive-parent`：把多版本草稿归档到一个父目录（`版本1/版本2/...`），核验后回写收据
- `table-update`：将“草稿目录/成片状态”等回写业务表（飞书 Base），使用 `LARK_BASE_TOKEN`
- `failure-update`：失败/部分失败回执，写“任务状态=生成失败”与“问题反馈”摘要；不写“草稿文件/成功状态/NAS 路径”
- 唯一维护实现为 `tools/workflow/closed_loop.py`；根 `closed_loop.py` 与 `tools/closed_loop.py` 是同一实现的兼容入口，使用一致的 QC、Windows 自动验收与分发收据闸门。普通导入共享模块状态；两条路径支持包外工作目录直接执行。

## 3. 评估与收据（`tools/*.py`）
- `paper_selection_replay.py` / `selection_preview.py`：复用已分析镜头、固定原音轨和句长的纸品选镜 A/B 验证，只输出预览与未解决项；不执行生产建稿或交付。见 [纸品选镜验证](../paper-selection-replay.md)。
- `tools/acceptance/{evaluation,task_review,delivery_gate,finalization}.py`：四项当前证据验收、正式交付前检查和交付协调；`production_eval.py` 只兼容旧导入，`eval_bootstrap.py` 可独立冻结输入。
- `material_index.py/material_library.py`：素材索引/库工具（不在 v1.5.1 正常路径写入素材库）
- `classify_feedback.py`：将业务反馈归类到可复用规则候选，产出可审计规则文本
- `feedback_candidates.py`：只追加私有反馈候选与人工审核/回归/发布事件，不在运行中提升全局规则
- `sentence_review.py`：通过自身 `collect` 命令收集私有句级审稿，并冻结输入、调用实际 matcher 离线回放和比较；命令见当前包 `docs/sentence-review.md`，人工标签不授予正式交付许可
- Mac 交接、人工 prepare/collect 和旧批准文件不再进入运行链。

## 4. 已退役的 Windows 入口

`run_task.template.ps1`（JyRun）与 `jy_poll.ps1`（JyPoll）不再执行任务，调用即非零退出并提示迁移。它们不读取凭据、创建任务目录、挂载 NAS、调用 worker 或发送通知。

单条任务按[快速开始](../quickstart.md)使用 manifest CLI；现有批队列从 `scripts/run_pipeline.ps1` 进入，内部复用 dispatcher。此改动不自动开启队列，不修改宿主计划任务，也不替换已经生成并使用中的生产脚本。

## 5. 测试
- `packages/video-v1.5.3/tests/`：包级测试（评估/队列/守卫/索引）
- `doubao-jianying-orchestrator/tests/test_task_control.py`：done.json 合同/阶段路由/skip/hold 行为

## 6. 运行时服务边界

- 视觉、描述匹配和文字复核只调用 GLM 或 Qwen 直连 HTTP 接口，具体 transport 别名与配置见 [模型与视觉流程](../model-and-vision.md)。
- `legacy` 与 `rule_then_ai` 保留选镜策略；它们不会启动已移除的 Codex CLI、Anthropic 或 CCSwitch 通道。旧 transport 和未知值明确拒绝。
- worker 的纯输入、失败分类、任务选择与并行互斥合同位于 `tools/queueing/contracts.py`，调度和执行继续从正式 dispatcher 进入。
