# 子系统：运行时与恢复（runtime-and-recovery）

- 代码：`packages/video-v1.5.3/doubao-jianying-orchestrator/orchestrator/runtime/{capacity,resource_profile,task_log,platform_env}.py`；诊断/安全检查位于 `orchestrator/quality/{diagnostics,draft_safety}.py`，官方迁移位于 `orchestrator/delivery/migration.py`

## 1. 职责
- 资源画像/容量标定/进程权重：避免与交互式剪映抢资源
- 统一任务日志与终态合同：何时终止、是否可重试、是否可安全继续
- 草稿写入前/后防线：不覆盖旧稿；写后再读校验结构/媒体/时间轴
- 诊断与只读恢复：不驱动 UI、分阶段恢复而不重新做视觉分析
- 加密草稿官方迁移：只调用剪映官方副本，不解密/不覆盖

## 2. 容量与资源（`capacity.py`，`resource_profile.py`）
- `calibrate_capacity`：隔离逐步增加“源素材数量”，记录系统资源/剪映进程响应；持久化 `host_capacity.json`
- 默认运行档：有剪映进程时 `background_friendly`（线程/并发降低、进程 nice 降权），否则 `performance`

## 3. 任务日志与终态合同（`task_log.py`）
- `TaskLogger`：`report/tasks/<run_id>` 内写 `task.json`、`events.jsonl`、`result.json` 与 `last_result.json`
- `result_contract(result, task_id, run_id, ...)` 增补稳定字段：
  - `terminal_state`: SUCCESS / UNCERTIFIED / BLOCKED / ERROR / DEFERRED
  - `failure_class`: vision / provider / timing / environment / ui / draft / semantic / unknown
  - `failure_code`: 例如 `VISION_CALL_FAILED` / `AUDIO_VIDEO_MISMATCH` / `ENV_ERROR`…
  - `retryable`: 仅 “视觉/服务商” 且具“瞬时”特征（超时/502/网络）才为 True
  - `safe_to_continue`: 只有 `terminal_state=SUCCESS` 且非中间态时为 True
  - `next_action`: complete / retry_stage / fix_environment / manual_review / skip_task
  - 附带：`pipeline_version`（读包内 VERSION）、`manifest_sha256`、`runtime`（Python/FFmpeg、实际视觉 transport/model 或配置错误；不再读取旧视觉 profile）

## 4. 安全写稿（`draft_safety.py`）
- 写前：若剪映运行且目标草稿已存在 → `BLOCKED`；加密草稿一律不覆盖（`DRAFT_ENCRYPTED_UNSUPPORTED`）
- 写后：独立再读 `draft_info.json/draft_meta_info.json`、验证时间线无重叠、媒体存在、视频/音频不越界、字幕样式位置、BGM 跟随、草稿索引同步（`root_meta_info.json`）

## 5. 只读诊断与分阶段恢复（`diagnostics.py`, `cli.recover`）
- 诊断：不驱动 UI，只读草稿/媒资/磁盘状态/包签名/依赖，报告 `DESKTOP_IMPORT_BLOCKED` 等
- 恢复：`preflight`（素材/环境就绪）→ `semantic_match`（消费已有 `shot_candidates.json` 与匹配报告）→ `draft_write`（严格只消费匹配缓存）→ `ui_acceptance`（只读桌面可见性诊断，保留 UNCERTIFIED，不授予正式交付）
- 默认 worker 对选镜服务超时/限流保留原始错误，并指向 `semantic_match`。开始选镜前冻结 `selection_resume.json` 中的任务、真实配音、句长和文件状态；恢复复用已完成观察，不重合成配音或重扫全库。写稿绑定原音频，同时更新匹配报告与片段，使草稿和独立QC消费同一结果。
- `selection_recovery_state.json` 按原始 `run_id` 持久记录一次阶段恢复预算。阶段失败或进程中断后仍保留预算，不因 preflight 的中间成功而回到完整 build；输入/音频变化、缓存损坏或预算耗尽均明确阻断。显式新建稿归档旧恢复状态；新原始运行使用新预算。选镜已完成后的BGM/写稿超时保留成功匹配，指向 `draft_write`，不重新选镜。

整体脚本另有任务级恢复与有限重试，见 [Windows 流水线](../windows-pipeline.md)。正式交付依据 `tools/acceptance` 的四项当前证据，不消费 Mac/Gold 人工批准文件；`ui_acceptance` 兼容诊断与这条自动验收链分开。

## 6. 加密草稿迁移（`migration.py`）
- 不解密/不复制密文；仅打开剪映官方客户端，由人执行“副本/导入”；工具轮询新草稿出现后做结构校验与来源快照一致性校验

## 7. 结果合同示例（精简）
```json
{
  "status": "AUDIO_SELECTION_REQUIRED",
  "terminal_state": "BLOCKED",
  "failure_class": "provider",
  "retryable": false,
  "safe_to_continue": false,
  "next_action": "manual_review"
}
```

## 8. 测试
- `test_task_control.py`：合同字段、阶段状态、BLOCKED/UNCERTIFIED 效果、错误/重试分类
- 包级 `test_selection_stage_resume.py`：真实 worker 到三阶段 CLI 的调用、原配音绑定、中断/耗尽不回退完整 build、运行身份和完成后超时分类
- `test_capacity_and_sync.py`：恢复/持久化/容量校验

