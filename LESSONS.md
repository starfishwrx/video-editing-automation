# LESSONS.md — 错题本（Agent 写，人审）

规则：
- 每条教训一个条目；再次遇到同类问题时不新建，在原条目 `hits` +1 并追加证据链接。
- `hits >= 3` → 开 PR 升级为硬规则（AGENTS.md / docs/code-standards.md / 测试），条目状态改 `PROMOTED`，写明落点。
- 只写“下次怎么避免”，不写长篇过程。不写任何凭据、NAS 路径、业务表链接。

## 新增条目

### L-20261026-01 配音身份和最终字幕时间必须共用同一输入
- status: OPEN
- hits: 1
- 模块: audio-voice / orchestrator-engine
- 现象: 预合成与最终合成分别读取任务和环境音色，会用另一说话人的时长选镜；按视频段写整句字幕会在换镜时重播文本。
- 根因: 音色/倍速解析分散，字幕依赖画面片段而非最终旁白。
- 以后怎么做: 显式任务音色先锁一次，倍速只处理原始缓存一次；名称与品类默认共用拆解入口，覆盖表格裸小数和“倍速”尾缀。字幕取最终纯旁白词时间，品牌和内容不符保持待审。结构回读与剪映批量编辑验收分开，CUDA视频可用也不代表ASR所需DLL齐全。
- 证据: `docs/meeting-features-20261026.md`、`test_meeting_voice.py`、`test_audio_captions.py`、`test_meeting_caption_integration.py`。

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

### L-20261026-01 API 槽位不是工作线程，媒体与请求阶段独立控制
- status: OPEN
- hits: 1
- 模块: visual-matching / runtime-and-recovery
- 现象: 放宽视觉槽位仍没有体感提速；进度字段也可能沿用上一轮的并发数。
- 根因: 槽位只限流，不创建请求；媒体准备占用请求工作线程会压低吞吐，整批收集返回值又推迟断点保存。跨进程槽位与每任务工作线程不是同一预算。
- 以后怎么做: 媒体准备先独立限流并完成，再启动请求池；请求按完成顺序校验并持久化，归并时按批次及镜头身份绑定。提交前记录实际并发配置，分阶段计时；不以离线模拟宣称生产加速。
- 证据: `test_vision_stage_concurrency.py`、`test_visual_qc_response_contract.py` 与包内 `ENVIRONMENT.md`。

### L-20261026-01 硬拒帧不能报成功；Path.glob 不能当布尔值
- status: OPEN
- hits: 1
- 模块: visual-matching
- 现象: 黑场 / 过曝 / 纯色全部硬拒时选帧仍 `ok=True`，坏帧进入视觉和相位 contact sheet。批量抽帧在 `-fps_mode` 失败且工作目录为空时不改试 `-vsync`。
- 根因: `pick_best` 全拒时回退到被拒帧，`_finalize_choice` 只要有路径就报成功。`bool(Path.glob(...))` 对生成器恒为真，空目录也被当成已有输出，兼容重试走不到。
- 以后怎么做: 全硬拒时外层 `ok=False`，调用方只收 `ok` 帧；准入口径变更升 `FRAME_QUALITY_VERSION`（当前 `frame-quality-v2`），让视觉 `frame_protocol` 与 `phase-timing-v3` 的 quality 字段换缓存键。判断有没有抽出文件用 `any(path.glob(...))` 或 `next(..., None)`。逃生阀写清楚：`JY_FRAME_QUALITY=0` 只按抽帧成败；`JY_FRAME_BATCH=0` 逐帧抽取。`SAMPLE_MAX` 仍是 12（`JY_VISION_SAMPLE_MAX` 可覆盖）。离线单测不代替成片或生产验收；质量门打开且合格帧不足 2 张时，视觉可能回落未过硬门的 `frame_paths`，相位则 `PHASE_FRAME_EXTRACT_FAILED`。
- 证据: PR #19 环审 https://github.com/starfishwrx/video-editing-automation/pull/19#pullrequestreview-5648674977 ；squash `7c733237c19086694460ab66e84c7b079f6f30dc`；修复 PR #20

### L-20261024-01 模型全名、默认流程与入口文档必须同步
- status: OPEN
- hits: 1
- 模块: visual-matching / docs
- 现象: 视觉配置实际为 `glm-5.3-flash`，注释却写 GLM 5.3，入口文档仍把旧 Top3/QC v3 当作默认；缺模型变量和可运行样例，版本目录也容易被当成真实版本。
- 根因: 只更新实现和包内局部说明，没有同步首页、环境合同、架构与协作入口。
- 以后怎么做: 型号保留 GLM-5.3-Flash 全名和 API ID；默认 transport/选镜/QC 变更同步模型、配置和架构文档。明确 legacy/实验范围，分别记录源码、发布附件和生产身份；用合成抽帧验证入门命令。
- 证据: `docs/model-and-vision.md`、`docs/quickstart.md`、`CONTRIBUTING.md`、`.github/workflows/ci.yml`。
### L-20261023-08 品类规则要到达看图与最终写入，配置值不能代替成片验证
- status: OPEN
- hits: 1
- 模块: visual-matching / orchestrator-engine
- 现象: 品类已有规则但AI入口未读取；固定字幕坐标与写后检查不能支持像素位置；只看时间轴相接仍可能漏掉音频内静音或提前结束后的空白。
- 根因: 规则、规划、写入和验收各用一套口径。
- 以后怎么做: 使用同一生效规则身份；规划偏好传到engine；字幕统一换算；按实际音频切片测量句间空白；未认证不得当成正式通过。
- 证据: 活包纸品规则、production_parameters与纸品回归测试。

### L-20261023-07 AI选镜改造必须贯穿末端、队列和独立QC
- status: OPEN
- hits: 1
- 模块: visual-matching
- 现象: 模型推荐正确仍被旧词表否决；局部新输出又被worker旧字段合同阻断。
- 根因: 推荐、实际分配、最终源窗和交付消费各自裁决；宽泛的观察区间也会使短口播无解。
- 以后怎么做: 用正常入口验证统一收据，明确legacy优先级；AI给出最短必要证据，程序检查源窗及跨句重复，QC独立查看最终片段。
- 证据: packages/video-v1.5.3/docs/AI_EDITOR.md；test_ai_editor.py；test_ai_editor_qc.py

### L-20261023-06 整词字幕口径必须回灌技能包，不能只停在流水线机
- status: OPEN
- hits: 1
- 模块: orchestrator-engine
- 现象: 批次按 15 字硬切把 `HelloKitty` 从中间切开；实验包闸门仍用裸 `len(text)`，与流水线 `subtitle_units` 不一致。
- 根因: 整词表与 `text_units` 只落在 `jy_pipeline` 部署，未进 `packages/video-v1.5.3`。
- 以后怎么做: 口播计字/拆句改动先进包（`subtitle_units` + 引擎闸 + 单测），再部署流水线；交付脚本只消费包内口径。
- 证据: PR #11；历史对话「Project scoring model」断句碎片 `HelloKitt`/`y`

### L-20261023-05 配音不留气口是全局规则，不能只改卫品
- status: PROMOTED(rules/global.json voice.max_gap_frames=1)
- hits: 1
- 模块: audio-voice
- 现象: 纸品成稿口播轨句间约 50ms 空段，卫品为 0；运营以为「不留空」已全品类生效。
- 根因: 1.5.2 仅把卫品 `max_gap_frames` 改为 1，全局/纸品仍 5 帧。
- 以后怎么做: 气口预算改 `rules/global.json`；品类包只叠加倍速等差异，不再各自保留更大气口。
- 证据: PR #10；抽检 zhipin 草稿 TTS gap≈50ms vs weipin gap=0

### L-20261023-04 分发和归档必须验证最终引用，部署必须包含校验模块
- status: OPEN
- hits: 1
- 模块: package-tools
- 现象: 目录存在或复制标记成功，接收稿仍可能依赖旧交付目录；只升级分发脚本会遗漏它实际导入的校验模块。
- 根因: 修复入口与日常收集入口不一致，归档只复制不重绑定路径，安装检查只看部分文件与版本号。
- 以后怎么做: 正常分发和修复共用实际引用收集；目的端暂存核验后发布；归档使用自身最终媒体路径；两个执行位置都按发布清单核对依赖和字节。不同会话使用独立工作树，提交前核对 HEAD。
- 证据: `packages/video-v1.5.3/docs/NAS_DELIVERY_STABILITY.md`、`tests/test_draft_delivery.py`、`tests/test_delivery_runtime.py`。

### L-20261023-03 通过状态必须由当前观察和当前输出重新验证
- status: OPEN
- hits: 1
- 模块: visual-matching / package-tools
- 现象: 缺字段的视觉响应、单包规格或换镜前的证据状态可能获得正式通过；旧参考片缺口又会误判当前素材永远不可用。
- 根因: 观察、证据合同、排序、场景重排和交付消费者使用不同判据；任务反馈尚未形成可重复回放的句级标准。
- 以后怎么做: 对当前观察逐项验证功能和数量要求，重排后重新验合同，QC消费者复核同一响应合同及草稿身份；未定义判据与素材缺口分开，人工允许/拒绝的候选与时间窗留在私有评估账本。
- 证据: `packages/video-v1.5.3/docs/QUALITY_CONTRACT_FIXES.md`；`test_v156_quality_ranking.py`、`test_visual_qc_response_contract.py`、`test_sentence_review.py`。

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
