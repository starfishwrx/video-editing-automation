---
name: doubao-jianying-orchestrator
description: Build and recover editable Jianying video drafts from task manifests using evidence-based shot matching, narration timing, visual QC, and human review gates. Use with the sibling jianying-editor skill for video automation.
---

# Doubao/Jianying orchestrator v1.5.6

纸品生产规则见包根`docs/PAPER_PRODUCTION_RULES.md`：13类语义视觉合同共用于分析、AI选镜和独立QC；
口播默认1.3倍、清洁画面优先1.3倍、硬切、实际气口不超过5帧、字幕像素X=0/Y=-528。
规则身份改变后旧AI观察和QC不自动复用。分段AI视频生成仅为素材制作规范，本包不新增生成服务。

完成标准：产出可打开、媒体可读、旁白与画面时长匹配、字幕内容正确的剪映草稿，
并记录独立视觉 QC 的结果。模型失败或证据不足保留待审状态，不虚报正式完成。
任务级 Eval、Gold 对照和 Mac 审阅按已配置的生产流程启用；它们不是普通源码修复的前置仪式。
源码合并以代码审查和离线回归为准，生产安装与真实成片验收分别记录。
哈希只用于需要复用的缓存或运输完整性，不要求源码、模型快照、提示词、拼图和收据逐层签名。

正式质量与故障恢复合同见
[references/production_quality_and_lessons.md](references/production_quality_and_lessons.md)。
formal 不得用 settled/lead_in/黑屏冒充完整动作；派生卖点标签不是视觉重看。

新任务默认使用`ai_editor`：先安排具体卖点，再用同商品包装、拆包及本体展示承接
普通介绍/钩子，后者正常匹配。体验表达允许贴切展示，明确性能结果及数值比较仍须
画面支持。旧词表不裁决新模式语义，最终切片独立QC使用v4，旧报告不能自动认证。
调用链、逐轮8候选、缓存及显式legacy回滚见包根`docs/AI_EDITOR.md`。

会议功能：先按音色表锁定 speaker，再应用一次 TTS 倍速（显式1.0有效）；兼容“真人播客女1.2”和“真人播客女1.2倍速”，裸整数不从名称拆走。
新 AI 草稿字幕沿用原文，按完成倍速、静音和响度处理后的每句 TTS 音频真实时长写入。
同一句跨多个镜头只显示一次；长口播先规划成可读句段，再合成，不按字数伪造词级时间。
不使用 ASR，不下载识别模型，也不把识别错字写回文案。画面速度2倍为偏好，最高20倍；
原素材中后段取段和去重不得挤掉唯一卖点证据。字幕写统一原生轨，结构回读不代替
剪映全选移动和保存重开验收。标准任务一文案一视频；人工参考字段评价保持 unknown。

近期人工审稿对应的打分、选镜与实验组句行为见包根
`docs/SELECTION_REVIEW_20261022.md`。语义观察和实际窗口先于去重；包装标称只支持对应标称，不替代功能实验。

legacy 的 QC v3 响应、旧场景重排复验及历史人工反馈回放见包根
`docs/QUALITY_CONTRACT_FIXES.md` 和 `docs/sentence-review.md`。
旧 QC 报告不能自动获得新合同认证；未定义合同先校准判据，不直接要求补拍。

这是视频自动化执行引擎，不包含业务账号、机器路径或生产日志。

草稿素材丢失、NAS/ZIP 交付或加密历史稿恢复，先读
[草稿分发与生成版恢复](references/draft_delivery_and_recovery.md)。
相对运输包需 import；共享交付必须检查实际绝对路径。加密时间轴不得自动换成旧明文，
仅在用户确认恢复原始生成版后使用包内 `tools.draft_delivery restore-generated`。

正常分发必须走 `ship_one` 的官方收集流程与授权 finalizer；多版本归档使用自身最终媒体路径。安装后运行 `tools.delivery_runtime` 对 pipeline 与 skills 两个执行面逐文件核对，不得仅凭 VERSION 或目录存在报告分发成功。行为和恢复边界见包根 `docs/NAS_DELIVERY_STABILITY.md`。

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
- 视觉默认直连火山 Ark `glm-5.3-flash`（`JY_VISION_TRANSPORT=volc_glm`），密钥放
  `ARK_API_KEY`（或 `JY_VOLC_API_KEY`）。批次与槽位并发默认 20。`codex` /
  `anthropic` 只作显式 legacy 回滚（需同时选择 legacy；AI editor 要求直连）；完整型号是 GLM-5.3-Flash，不是 GLM-5.3 或 ChatGLM3。媒体解码默认全部走 GPU（`JY_GPU_DECODE_CODECS=*`，
  单帧默认开启），显存不足或槽位超时仍回退 CPU。
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

旧场景剪辑规则（legacy / v1.5.3；全局配音默认值仍适用）：

- 规则分两层：`orchestrator/rules/global.json` 所有品类都守，`weipin.json` / `zhipin.json` 只放该品类自己的指令。品类按 `category` 字段，没有时按商品名识别。卫品指令只用于卫品。
- 默认硬切、不加转场；配音去首尾静音，全局不留句间气口（`max_gap_frames=1` / `tail_pad_s=0`）；卫品配音 1.2 倍速。
- 卫品场景（吸水实验、撕包装、透灯光、荧光测试、正面指产品、背标、面层）在选镜后按动作相位重新定窗，结果看 `scene_rules_report.json`。
- 同一任务的不同版本尽量不用同一批镜头；台账在 `work/.cache/shot_usage_*.json`。

旧预览降级（legacy / v1.5.1）：

- 正式模式仍按 `DIRECT → ACCEPTABLE_DEGRADED → MATERIAL_GAP`。降级镜不能抢走已有直证。棉、一条顶多片、侧漏、囤货量感这些硬口播没有直证就保持缺口，不能用包装特写冒充。
- 预览可以先按规则留下最多 3 句黑屏。超出 3 句的，只用还没出现过的已就绪镜头补上。剩下的黑屏再用和图文相关的镜头或产品展示镜填上。没有未用过的源文件时，才允许重复最合适的那条，并标成 `REUSE`。这些补镜不是证据，草稿仍是未认证预览。
- 自动预览不得把表格改成已完成。操作者看过效果、明确接受后，才可以替换上一版链接。之后的本地重跑不得把已完成改回生成失败。
