# 子系统：队列与闭环工具（package-tools）

- 代码：`packages/video-v1.5.1/{batch_worker.py, closed_loop.py, tools/*.py, run_task.template.ps1, jy_poll.ps1, gold-reference.json}`

## 1. 队列与执行（`batch_worker.py`）
- 工作根：`work/batch_queue.json`（记录列表）；每条任务落 `work/<record_id>/`
- `done.json` 为权威终态收据；历史 start 事件/活跃进程都会被它覆盖
- 环境变量控制：
  - `JY_TASK_SKIP_CURRENT=1`：跳过当前卡住的条目并选下一条
  - `JY_TASK_RECORD_ID`：仅执行指定记录
  - `JY_TASK_FORCE_REBUILD`：强制重建（谨慎使用）
- 关键文件：`progress.jsonl`（事件流）、`task_status_snapshot.json`（当前权威状态）
- 失败分类（`classify_build_failure`）：区分确定性/瞬时/提供商/环境，供上层回退与通知

## 2. 闭环与交付（`closed_loop.py` 与 tools）
- `audit`：读取 `task.json/done.json` 与 `report/draft_visual_qc.json`，按策略校验是否满足正式交付（QC=PASS、Mac 审阅绑定、包完整）
- `archive-parent`：把多版本草稿归档到一个父目录（`版本1/版本2/...`），核验后回写收据
- `table-update`：将“草稿目录/成片状态”等回写业务表（飞书 Base），使用 `LARK_BASE_TOKEN`
- `failure-update`：失败/部分失败回执，写“任务状态=生成失败”与“问题反馈”摘要；不写“草稿文件/成功状态/NAS 路径”
- v1.5.1 同时存在：
  - 根目录 `closed_loop.py`：精简依赖，仅按 QC/审核/打包做交付 gate
  - `tools/closed_loop.py`：集成评估/分发收据/目录哈希（`tools/production_eval.py`、`delivery_identity.py`）
  - 二者服务不同部署场景（Windows 工具链 vs. 更完整的评估闭环），脚本名相同但实现与依赖不同

## 3. 评估与收据（`tools/*.py`）
- `eval_bootstrap.py/eval_stage.py/production_eval.py`：生产评估流程与证据（contract 与 receipt 校验）
- `material_index.py/material_library.py`：素材索引/库工具（不在 v1.5.1 正常路径写入素材库）
- `classify_feedback.py`：将业务反馈归类到可复用规则候选，产出可审计规则文本
- `mac_review_handoff.py`：Mac 端人工审核交接脚本

## 4. Windows 脚本
- `run_task.template.ps1`：按模板运行一条任务（含日志/证据路径与环境变量）
- `jy_poll.ps1`：Windows 轮询与队列调度脚本

## 5. 测试
- `packages/video-v1.5.1/tests/`：包级测试（评估/队列/守卫/索引）
- `doubao-jianying-orchestrator/tests/test_task_control.py`：done.json 合同/阶段路由/skip/hold 行为

## 6. 已知差异：两个 closed_loop.py 的关系
- v1.5.1 下同时存在 `packages/video-v1.5.1/closed_loop.py` 与 `packages/video-v1.5.1/tools/closed_loop.py`
  - 前者为“精简版闭环 gate”，仅依赖 QC + Mac 审阅结果与包完整性；不引入生产评估/目录身份哈希
  - 后者为“评估闭环版”，在成功 gate 之外还核对 `production_eval` 合同、分发收据的哈希绑定等
  - 这两个脚本服务于不同的落地形态（Windows 本地工具链 vs. 带评估收据的产线），并不会在同一流程中同时调用

