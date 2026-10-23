# LESSONS.md — 错题本（Agent 写，人审）

规则：
- 每条教训一个条目；再次遇到同类问题时不新建，在原条目 `hits` +1 并追加证据链接。
- `hits >= 3` → 开 PR 升级为硬规则（AGENTS.md / docs/code-standards.md / 测试），条目状态改 `PROMOTED`，写明落点。
- 只写“下次怎么避免”，不写长篇过程。不写任何凭据、NAS 路径、业务表链接。

## 条目模板
```
### L-YYYYMMDD-NN 一句话标题
- status: OPEN | PROMOTED(<落点>) | OBSOLETE
- hits: 1
- 模块: orchestrator-engine / visual-matching / ...
- 现象: 
- 根因: 
- 以后怎么做: 
- 证据: PR/issue/commit 链接
```

## 已有条目（从代码注释与 references 中回填的种子，hits 按已知次数）

### L-20261022-02 新版本必须回收已验证的选镜热修
- status: OPEN
- hits: 1
- 模块: visual-matching
- 现象: 新候选沿用旧规则，正确的抽数/层数候选被旧闸门挡住，开场展示提前消耗后续直证。
- 根因: 工具包版本升级与业务规则回流分离；排序、复用、场景相位和最终语义门采用不同口径。
- 以后怎么做: 发版对照人工审稿案例与已验证修复，冻结文案、观察、模型响应及真实音频回放；验证候选实际窗口，缺证不得靠抬分通过。多镜组合共享证据门，明确反证在所有兜底前排除。
- 证据: `packages/video-v1.5.3/docs/SELECTION_REVIEW_20261022.md`、`test_v156_selection_feedback.py`、`test_v156_feedback_semantics.py`。

### L-20261022-01 视觉默认直连后 Codex 单测必须显式钉 transport
- status: OPEN
- hits: 1
- 模块: visual-matching
- 现象: 默认 `volc_glm` 后，只 mock `_run_codex_file` 的匹配用例 call_count=0。
- 根因: unset `JY_VISION_TRANSPORT` 已不再走 Codex。
- 以后怎么做: Codex 回滚路径测试在 setUp 写 `JY_VISION_TRANSPORT=codex`；直连默认另开 `test_volc_glm_vision`。
- 证据: `packages/video-v1.5.3/doubao-jianying-orchestrator/tests/test_codex_invocation.py`

### L-20261016-01 副本哈希一致不代表媒体路径有效
- status: OPEN
- hits: 1
- 模块: package-tools / runtime-and-recovery
- 现象: ZIP 和 NAS 副本完整，接收方剪映仍显示媒体丢失；旧收据续跑也可能跳过修复。
- 根因: 文字引用仍指向失效目录，或将相对运输包当成可直接打开的共享交付稿；旧明文 sidecar 也不代表当前加密时间轴。
- 以后怎么做: 分发、续跑和 enforce 回填审核均检查实际时间轴的绝对路径与字节。恢复原生成版需要用户确认、成功任务证据、新 UUID 和编辑内容比较。维护入口为 `tools.draft_delivery`，合包验收入口为 `tools.release_package`。
- 证据: `packages/video-v1.5.3/tests/test_draft_delivery.py` 与包内合包技能。

### L-SEED-01 版本闸门要用“下限”不能用“等值”
- status: OPEN
- hits: 1
- 模块: orchestrator-engine
- 现象: 1.3.14–1.3.22 期间 `MIN_SUPPORTED_PACKAGE_VERSION` 被写成等值比较，每次升版都让环境自检失败，且藏了九个版本没发现（engine.py 注释）。
- 以后怎么做: 版本比较一律走 `_version_key` 做 >= 比较；新增闸门必须有“新版本能通过”的测试。

### L-SEED-02 4K HEVC 的 CUDA 解码必须带 `-threads 1`
- status: PROMOTED(RULES.md 交付与审计)
- hits: 1
- 模块: media-codec
- 现象: 不加时 ffmpeg 退出码 0 但静默退回 CPU。
- 以后怎么做: 只看退出码不够，要校验实际使用的解码器。

### L-SEED-03 shot_analyzer 读不到引擎内对 delivery_mode 的归一化
- status: OPEN
- hits: 1
- 模块: orchestrator-cli / visual-matching
- 现象: 选镜在引擎构造前就跑完，引擎里写回 manifest 的 `delivery_mode` 对它无效（engine.run 注释）。
- 以后怎么做: 影响选镜的字段必须在 `cli.cmd_build` 侧先写入 manifest。

### L-20261023-03 通过状态必须由当前观察和当前输出重新验证
- status: OPEN
- hits: 1
- 模块: visual-matching / package-tools
- 现象: 缺字段的视觉响应、单包规格或换镜前的证据状态可能获得正式通过；旧参考片缺口又会误判当前素材永远不可用。
- 根因: 观察、证据合同、排序、场景重排和交付消费者使用不同判据；任务反馈尚未形成可重复回放的句级标准。
- 以后怎么做: 对当前观察逐项验证功能和数量要求，重排后重新验合同，QC消费者复核同一响应合同及草稿身份；未定义判据与素材缺口分开，人工允许/拒绝的候选与时间窗留在私有评估账本。
- 证据: `packages/video-v1.5.3/docs/QUALITY_CONTRACT_FIXES.md`；`test_v156_quality_ranking.py`、`test_visual_qc_response_contract.py`、`test_sentence_review.py`。
