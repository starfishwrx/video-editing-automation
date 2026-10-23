---
name: doubao-jianying-orchestrator
description: Build and recover editable Jianying video drafts from task manifests using evidence-based shot matching, narration timing, visual QC, and human review gates. Use with the sibling jianying-editor skill for video automation.
---

# Doubao/Jianying orchestrator v1.5.5 experimental

本包是隔离测试版本，继承的生产合同仍有效；未部署到已安装技能或队列。
使用包根 `scripts/run_v154_test.py`，显式提供 input、report-dir、index-dir、
model-cache-dir 和 drafts-root。先 `uv sync --frozen --group indexing`；模型
权重独立缓存，不包含在 ZIP，不修改全局环境或生产草稿库。

旧 manifest 默认 `pipeline_strategy=legacy`；测试启动器明确设置
`indexed_contextual`，经正常 `run.py build` 完成真实配音冻结、本地素材检索、
相关候选核验、整稿连续动作规划、实际切片观察及草稿回读。已冻结文件配音不再
重新解析音色。SigLIP 只负责召回，GPU batch 默认 8；新要求只补查相关缺失证据，
旧描述和规则戳不能自动证明新要求。候选 8 条不足时每组最多扩大至 16 条，
审核只修失败组且最多两轮，不改原文或冻结音频，不重看全库。

锁定计划不得再让旧逐句换镜或场景补镜覆盖。缺证或动作/时长无解必须保留具体
句组问题；不能用包装、定住或黑屏替代功能动作。模型看到的实际切片序列不等于
人工接受，所有本地试跑保留 `formal_delivery_allowed=false`、`human_review=PENDING`。
审核页及哈希绑定反馈仅用于实验与案例积累，不自动部署、发 PR、分发 NAS 或回填飞书。
详细计划见包根 [docs/V154_UPDATE_PLAN.md](../docs/V154_UPDATE_PLAN.md)。

For new production tasks, freeze task-level Eval inputs and runtime identity
before generation. Use the reviewed category/SKU material index when configured;
keep visual rules separate from the generic Diaodu/RDM contract. Retain real
QC, Gold-quality and Mac-review evidence. Observation mode never fabricates a
PASS and does not replace existing delivery gates.

正式质量与故障恢复合同见
[references/production_quality_and_lessons.md](references/production_quality_and_lessons.md)。
formal 不得用 settled/lead_in/黑屏冒充完整动作；派生卖点标签不是视觉重看。

这是视频自动化执行引擎，不包含业务账号、机器路径或生产日志。

草稿素材丢失、NAS/ZIP 交付或加密历史稿恢复，先读
[草稿分发与生成版恢复](references/draft_delivery_and_recovery.md)。
相对运输包需 import；共享交付必须检查实际绝对路径。加密时间轴不得自动换成旧明文，
仅在用户确认恢复原始生成版后使用包内 `tools.draft_delivery restore-generated`。

用户确认修复有效并要求合入包时，读
[已验证修复合入发布包](references/validated_fix_to_release.md)，
使用包内 `tools.release_package` 完成清单、ZIP 和全新解压验收。
局部交付修复选 `--checks delivery`；发版 PR 选 `--checks full`，
无需每次局部改动都运行全量测试。

部署时由宿主环境提供：

- `JY_SKILL_ROOT`、`JY_DRAFT_ROOT`、`JY_INSTALL_ROOT`
- `JY_NAS_SHARE`、`JY_NAS_USER`、`NAS_PASSWORD`
- `JY_MAC_DRAFT_ROOT`
- `LARK_BASE_TOKEN`

引擎职责：素材理解、语义分段、证据匹配、稳定切片、字幕写入、草稿结构校验和可回放证据生成。它不负责把视觉业务规则升级为通用 Diaodu/RDM 规则。

执行边界：

1. 先生成素材理解和匹配证据，再写入草稿。
2. 任何视觉模型失败或证据不足都必须阻断正式 `PASS`。
3. 只重做失败语义段，保留已完成稿和 deferred indices。
4. 分发和表格回写必须经过独立人工检查凭证。
5. 版本号从运行时实际文件回读，禁止在提示词中手写版本。

安全继续与恢复：

- 编排器默认单任务运行；上一个任务没有完整结束收据，或结果不是
  `SUCCESS`，就停在当前任务，不自动进入下一条。
- `result.json` / `last_result.json` 使用固定的 terminal_state、failure_class、
  retryable、safe_to_continue、next_action 和 parent_run_id 合同；失败后的新
  attempt 必须关联父 run。
- 环境失败只重做预检；语义失败只消费已有分析做匹配或人工确认；写稿和桌面验收
  分别从对应阶段恢复，不重新做视觉分析。
- 视觉服务调用最多进行一次有限重试；凭据、路径、编码和余额错误直接停机。
- 视觉子进程默认使用渲染机 `.codex/volc.config.toml` 的 `volc` profile 和火山
  CodingPlan OpenAI-compatible 接口；CC Switch Anthropic env 文件只作为显式回滚路径。
- `draft_write` 只接受 `semantic_match` 产生的成功嵌套结果；原始 manifest 或损坏缓存
  不得作为恢复输入。桌面诊断失败保持 `DESKTOP_IMPORT_BLOCKED` / `PACKAGE_NOT_ACTIVE`。
- 批量跳过必须由操作者显式设置 `JY_TASK_SKIP_CURRENT=1`，并先写入
  `DEFERRED` 收据。
- 批量 `done.json` 必须同时带有 `task_id`、`manifest_sha256`、`runtime` 和
  `task_log`；`task_log` 指向的 report 根目录下必须存在带 `task_finished` 事件的
  `result.json`/`events.jsonl`，否则继续队列会停在当前任务。
- 队列选择后的生成前预检会验证包版本、素材目录与视频池、草稿根目录父路径和脚本；
  预检失败直接写 `ENVIRONMENT_PRECHECK_FAILED`，不得触发 TTS、视觉分析或建稿。

命令、运行路径和验收合同见 [references/runtime_contract.md](references/runtime_contract.md)。
预览留空补镜和这次生产踩过的坑见 [references/preview_gap_and_pitfalls.md](references/preview_gap_and_pitfalls.md)。
卫品 1.5.2 历史踩坑（含旧 preview/settled 口径）见包内 `docs/WEIPIN_1.5.2_PITFALLS.md`，仅作历史参考。

剪辑规则（v1.5.3）：

- 规则分两层：`orchestrator/rules/global.json` 所有品类都守，`weipin.json` / `zhipin.json` 只放该品类自己的指令。品类按 `category` 字段，没有时按商品名识别。卫品指令只用于卫品。
- 默认硬切、不加转场；配音去首尾静音，句间气口不超过 5 帧；卫品配音 1.2 倍速。
- 卫品场景（吸水实验、撕包装、透灯光、荧光测试、正面指产品、背标、面层）在选镜后按动作相位重新定窗，结果看 `scene_rules_report.json`。
- 同一任务的不同版本尽量不用同一批镜头；台账在 `work/.cache/shot_usage_*.json`。

预览降级（v1.5.1）：

- 正式模式仍按 `DIRECT → ACCEPTABLE_DEGRADED → MATERIAL_GAP`。降级镜不能抢走已有直证。棉、一条顶多片、侧漏、囤货量感这些硬口播没有直证就保持缺口，不能用包装特写冒充。
- 预览可以先按规则留下最多 3 句黑屏。超出 3 句的，只用还没出现过的已就绪镜头补上。剩下的黑屏再用和图文相关的镜头或产品展示镜填上。没有未用过的源文件时，才允许重复最合适的那条，并标成 `REUSE`。这些补镜不是证据，草稿仍是未认证预览。
- 自动预览不得把表格改成已完成。操作者看过效果、明确接受后，才可以替换上一版链接。之后的本地重跑不得把已完成改回生成失败。
