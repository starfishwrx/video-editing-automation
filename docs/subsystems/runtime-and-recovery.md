# 子系统：运行时与恢复（runtime-and-recovery）

- 代码：`packages/video-v1.5.1/doubao-jianying-orchestrator/orchestrator/{capacity.py, resource_profile.py, task_log.py, diagnostics.py, migration.py, draft_safety.py}`

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
  - 附带：`pipeline_version`（读包内 VERSION）、`manifest_sha256`、`runtime`（Python/FFmpeg/vision profile）

## 4. 安全写稿（`draft_safety.py`）
- 写前：若剪映运行且目标草稿已存在 → `BLOCKED`；加密草稿一律不覆盖（`DRAFT_ENCRYPTED_UNSUPPORTED`）
- 写后：独立再读 `draft_info.json/draft_meta_info.json`、验证时间线无重叠、媒体存在、视频/音频不越界、字幕样式位置、BGM 跟随、草稿索引同步（`root_meta_info.json`）

## 5. 只读诊断与分阶段恢复（`diagnostics.py`, `cli.recover`）
- 诊断：不驱动 UI，只读草稿/媒资/磁盘状态/包签名/依赖，报告 `DESKTOP_IMPORT_BLOCKED` 等
- 恢复：`preflight`（素材/环境就绪）→ `semantic_match`（消费已有 `shot_candidates.json` 与匹配报告）→ `draft_write`（严格只消费匹配缓存）→ `ui_acceptance`（只读诊断，人工验收保留 UNCERTIFIED）

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
- `test_capacity_and_sync.py`：恢复/持久化/容量校验

