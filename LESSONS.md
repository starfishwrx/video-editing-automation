# LESSONS.md — 错题本（Agent 写，人审）

规则：
- 每条教训一个条目；再次遇到同类问题时不新建，在原条目 `hits` +1 并追加证据链接。
- `hits >= 3` → 开 PR 升级为硬规则（AGENTS.md / docs/code-standards.md / 测试），条目状态改 `PROMOTED`，写明落点。
- 只写“下次怎么避免”，不写长篇过程。不写任何凭据、NAS 路径、业务表链接。

## 新增条目

### L-20261102-02 时间线末端与保护区必须核验真实输出帧
- status: OPEN
- hits: 1
- 模块: domain/timeline / media/timeline_render
- 现象: 非整帧配音若把整片帧数直接 floor，会有末尾旁白没有画面；名义源窗覆盖结果，也可能采不到所需结果帧。Matroska 的 60fps 还会因 1ms time_base 表现为 16/17ms 交替。
- 根因: 业务微秒、容器时间基和输出帧预算混用，或仅凭 avg_frame_rate 判断源时间分布。
- 以后怎么做: 内部切点共享累计 floor、整片末端 ceil，最后一帧必须有真实源画面；实际 PTS 在容器精度内核对 CFR，再核验选中源帧和保护区的输出帧数量。短源、缺帧、编码增删帧保持阻断，不靠尾帧克隆或截旁白补齐。
- 证据: `test_edit_timeline.py`、`test_timeline_render.py` 的非整帧、非零 PTS、逐帧像素及保护结果反例；实现边界见 `docs/programmatic-production.md`。

### L-20261102-01 冻结计划复用必须自行建立输出目录
- status: OPEN
- hits: 1
- 模块: application/cli / sentence-planning
- 现象: 已冻结的 semantic-v1 清单换新 report 目录构建时，在选镜前因 sentence_planning 目录不存在而失败。
- 根因: 首次规划器负责创建目录，复用计划会跳过该规划器；后续保存逻辑仍假定目录已经存在。
- 以后怎么做: 保存冻结计划的阶段自行建立目标父目录。用真实临时目录复现“已有分句 + 新报告目录”，确认不重新分句、原句序与配音时长保持，并确实到达匹配阶段。
- 证据: `test_sentence_planning.py::test_build_reuses_frozen_plan_in_fresh_report_directory` 先失败后通过；修复位于 `application/cli.py`，不向兼容入口写业务逻辑。

### L-20261030-04 句长可行性与动作完整性必须分别核验
- status: OPEN
- hits: 2
- 模块: selection / retiming / preview
- 现象: 全片平均倍速下降仍可能保留半截动作；模型也会把下一次入手说成结果静置，或把一个固定机位误称为两个角度。横向拉宽的拼图还会影响包装厚实判断。
- 根因: 仅凭历史描述、分段名称或时长合计不能证明实际使用窗口完整，短口播又容易诱导裁掉必要动作。
- 以后怎么做: 保持图片比例，按最终连续源窗口重新看图；保护关键接触、完成与结果，只压缩明确允许的中间过程。微秒精确求解后还要核对输出帧边界并播放 A/B。放不下就保留时长冲突，不把方案就绪当成质量 PASS。参考工程要标注当前版本与归档快照及缺失媒体。
- 证据: `test_paper_retiming.py`、`test_paper_selection.py`、`test_paper_review_media.py`、`test_selection_preview.py`、`docs/paper-selection-replay.md`。
- 再次遇到: 名义源窗到达结果，fps中心重采样后的实际尾帧却仍在展开。先以真实PTS和不带时长目标的观察记录动作，再搜索连续窗口与帧预算；实际输出重验源帧对应。新增 `test_paper_action_events.py`、`test_paper_action_windows.py` 与显式映射真实渲染测试，说明见 `docs/paper-action-windows.md`。

### L-20261030-03 定时流水线必须使用当前证据并隔离代码根与工作根
- status: OPEN
- hits: 1
- 模块: windows-runner / acceptance / delivery
- 现象: 旧流程信任 PASS 字符串或历史批准，可能先分发后验收；外置工作目录与旧宿主环境又会把子进程指向错误代码或 Python。
- 根因: 输入模式、验收与交付分支各自裁决，路径从工作目录反推，重复触发没有稳定的重试预算。
- 以后怎么做: 整体脚本只负责选任务和限制次数，正式放行共享四项当前证据检查且发生在所有交付动作之前。代码根、工作根和草稿根分开传递；子进程固定当前包和解释器。Windows 大小写同名记录不能重置预算，分发副本存在也不等于归档和回写完成。用真实本地夹具、跨进程锁和 PowerShell 参数往返验证这些边界。
- 证据: `tests/test_scheduled_pipeline.py`、`tests/test_acceptance_delivery_gate.py`、`tests/test_production_eval.py`、`docs/windows-pipeline.md`。

### L-20261030-02 删除模型后端要覆盖整条执行链和提前拒绝配置
- status: OPEN
- hits: 1
- 模块: vision / task-preflight / retired-runners
- 现象: 视觉旧通道删除后，历史调度模板仍能启动整条 Agent；直接建稿也可能先生成配音再发现通道不支持。
- 根因: 后端选择散落在模型调用、调度、运行事实和缓存中，失败诊断又依赖相同配置。
- 以后怎么做: 通道统一解析，诊断保存无效配置但不自行失败，建稿和生成恢复在任何收费准备前阻断。退役执行器在读配置/写任务/外发之前退出。保留通用证据、QC 和 HTTP 重试合同，用真实旧新缓存键对照验证清理没有引起整库重分析。
- 证据: `tests/test_vision_runtime_config.py`、`tests/test_retired_runners.py`、`doubao-jianying-orchestrator/tests/test_sentence_matching.py`、`docs/modular-development.md`。

### L-20261030-01 功能拆分必须同时验证导入身份与发布库存
- status: OPEN
- hits: 1
- 模块: module-layout / release-inventory / compatibility
- 现象: 直接复制导出会使旧 patch 与实现状态分离；通用 media 目录排除会把新媒体代码包也漏掉。开发树回归通过仍可能无法在全新包中启动。
- 根因: Python 模块对象和目录名称同时承担了运行时合同，单纯搬文件没有覆盖这些隐含依赖。
- 以后怎么做: 旧导入指向同一实现对象；数据与媒体操作围绕明确输入输出抽取；资源根独立于实现深度。每次引入功能子目录在包外副本做 full 合包/解压回归，并验证私有媒体排除仍生效。按文件加载的证据合同必须读取同一包副本。
- 证据: `tests/test_modular_architecture.py`、`tests/test_closed_loop_entrypoints.py`、`doubao-jianying-orchestrator/tests/test_media_boundary.py`、`docs/modular-development.md`。

### L-20261028-09 混合选镜与缓存抽帧的证据身份
- status: OPEN
- hits: 1
- 模块: visual-matching / independent-qc / draft
- 现象: 规则与 AI 混排草稿缺 AI 身份收据；原始素材复制进草稿后路径变化被误报为内容变化；旧观察的临时帧被清理后又误报证据缺图。
- 根因: 选镜、草稿复制和质检使用不同的身份口径，缓存观察绑定了临时路径。
- 以后怎么做: 混排时封存选镜身份，复制素材同时核对原件状态和完整内容哈希；从匹配的源素材与已审时间重建缺失帧，仍需独立视觉和人工验收。
- 证据: `test_ai_editor_qc.py`、`test_visual_match_recovery.py`。

### L-20261028-08 功能动作聚焦不能落在前置展示
- status: OPEN
- hits: 1
- 模块: visual-matching / scene-timing
- 现象: 湿水句选中了包含实验的素材，但主题聚焦先命中干纸展开，最终切片漏掉浸水和提起。
- 根因: 泛化“不破”主题与更具体的湿水主题共用候选动作，按时间顺序取到了前置动作。
- 以后怎么做: 针对明确提及的具体动作优先定位对应已观察分段；未覆盖的其他卖点保留未认证。对完整动作不能在速度要求内放下的情况报告缺口或更换素材，不静默压到极高倍速。
- 证据: `test_v156_selection_feedback.py`、`test_v152_meeting_rules.py`。
### L-20261028-10 提速不能改变证据与交付计数单位
- status: OPEN
- hits: 1
- 模块: 抽帧 / 调度 / staging / preview / throughput
- 现象: 全轮准备屏障、逐帧进程与中间复制增加成本；草稿PASS被误读成成片产量；部分预览收据丢掉未覆盖要求。
- 根因: 执行顺序与语义顺序耦合、复制身份只按同名串行假设、统计未明确artifact类型。
- 以后怎么做: 就绪执行按ID归并且有背压；抽帧先证明PTS对应；并发复制从一开始用独立目标名并原子发布；保留完整需求且只显式preview容忍不足；草稿与MP4分别计量，未知为null。
- 证据: `test_bounded_frame_batch.py`、`test_ready_work.py`、`test_single_copy_staging.py`、`test_ai_editor.py`、`test_throughput_calibration.py`；详见 `docs/plans/20261028-production-optimization.md`。

### L-20261028-07 已选镜头的保护区与卖点覆盖
- status: OPEN
- hits: 1
- 模块: ai-editor / timing / preview
- 现象: 已确认的源窗口在写稿时重复扣除片头保护，或一句多个卖点因部分画面缺证而不能预览。
- 根因: 保护区按选中窗口再次计算，且预览与正式证据覆盖要求没有分开记录。
- 以后怎么做: 保护区始终相对原场景计算；允许的部分覆盖只用于显式预览，逐项标记未确认内容，正式模式和独立质检不放宽。
- 证据: `test_paper_production.py`、`test_ai_editor.py`。

### L-20261028-06 模型召回可能缩写素材编号
- status: OPEN
- hits: 1
- 模块: visual-matching / ai-editor
- 现象: 宽召回返回 source 编号而非完整 shot 编号，整稿被候选不存在错误阻断。
- 根因: 模型输出格式与项目镜头标识不一致，但后续正式复看本就只接受真实镜头。
- 以后怎么做: 仅在 source 与真实镜头一对一时解析缩写，并记录解析与忽略项；歧义和未知不进入候选，像素复核及 QC 原样执行。
- 证据: `test_ai_editor.py` 的唯一和歧义编号测试。

### L-20261028-05 旧任务清单缺商品导致同素材缓存隔离
- status: OPEN
- hits: 1
- 模块: visual-matching / input-contract
- 现象: 旧任务记录有商品，而旧分析清单缺商品；相同素材的新任务带商品后视觉键不一致，重复识别。
- 根因: 商品身份只绑定在视觉键中，旧版编排没有贯通任务商品。
- 以后怎么做: 显式核对旧任务、清单、素材范围，并重算前后键仅差商品的部分；只导入有效视觉观察，文字仍按原状态补验，候选复看和 QC 不绕过。
- 证据: `test_legacy_cache_import.py`、`docs/subsystems/visual-matching.md`。

### L-20261028-04 共享缓存不能整份合并旧快照
- status: OPEN
- hits: 1
- 模块: visual-matching / runtime-and-recovery
- 现象: 两个商品同时处理时，一方更新文字复核结果，另一方把同一缓存中的旧记录写回，后续任务重新调用文字模型。
- 根因: 虽然持有文件锁，但每次仍用进程启动时的整份缓存覆盖磁盘的对应键。
- 以后怎么做: 在锁内只合并调用方实际修改的键；失败保留待写集合，缓存损坏时不静默覆盖。缓存仍是加速器，不替代当前证据校验。
- 证据: `test_v152_resume_integrity.py` 跨商品旧快照交错写入和损坏缓存测试。

### L-20261028-03 视觉分析不可用时不继续批量文字复核
- status: OPEN
- hits: 1
- 模块: visual-matching / runtime-and-recovery
- 现象: 某视觉批次空响应后，流程仍准备并启动大批量文字复核，而这次正式匹配已被该错误阻断。
- 根因: 视觉阶段错误与文字复核调度之间没有前置失败检查。
- 以后怎么做: 保存成功观察和失败详情，推迟文字复核并保持任务未认证；只有恢复了缺失的视觉证据后才继续。
- 证据: `test_material_cache.py` 的视觉失败延后文字测试。

### L-20261028-02 闭环成功状态必须等外部步骤完成
- status: OPEN
- hits: 1
- 模块: package-tools / runtime-and-recovery
- 现象: 归档或表格回写失败前已将本地 done 写成 OK。
- 根因: 外部工具需要待审核输入，却将它直接复用于最终状态快照。
- 以后怎么做: 用单独的内部候选文件承载闭环工具输入；归档和回写成功并收到有效结果后再发布 OK，失败保留待处理状态和已分发收据。
- 证据: `test_draft_delivery.py` 中闭环失败与回写收据测试。

### L-20261028-01 可编辑节奏和长字幕必须在配音前区分
- status: OPEN
- hits: 1
- 模块: timing / visual-matching / audio-voice
- 现象: 整稿中可按语义停顿拆开的长句在合成后遇到字幕长度阻断；唯一可用卖点镜头提前终止复核，丧失同等证据下的编排选择。
- 根因: 分句、音频时长、画面候选和字幕边界各自在不同阶段决定；表格音色“倍”后缀未纳入统一解析。
- 以后怎么做: 只在保留原文与数字的自然标点处、合成前规划可读句段；显式分句仍尊重原输入。必要时有上限地扩展候选，继续保留证据和窗口的未知状态；统一解析音色倍速。
- 证据: `test_reference_sentence_planning.py`、`test_ai_editor.py`、`test_meeting_voice.py`。

### L-20261027-01 并发槽位必须覆盖实际连接，重试预算必须跨恢复
- status: OPEN
- hits: 1
- 模块: visual-matching / runtime-and-recovery / package-tools
- 现象: 等待超时后后台请求仍存活，外层重复重试叠加在途量；不同入口各自扩大槽位；已成稿QC超时又整稿重建。
- 根因: 线程等待期限冒充HTTP总期限，准备/请求/父阶段混用预算，重试与状态分散在多个模块。
- 以后怎么做: 连接清理完成才释放账户槽，只有最终content可认证；一个适配层拥有持久两次总预算，所有生产入口共享策略并经调度准入。同产品冷准备作为依赖，逐批保存QC；缺文字只补文字，没有新输入停止重建。只统计独立PASS与验证交付的新增交集。
- 证据: `test_vision_client.py`、`test_runtime_policy.py`、`test_material_cache.py`、`test_batch_dispatch.py`、`test_visual_qc_response_contract.py`、`docs/production-throughput.md`。

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

### L-20261026-04 写入参数要同源，批次身份要跨轮询
- status: OPEN
- hits: 1
- 模块: captions / JyPoll
- 现象: 纸品配置字幕坐标与写入默认值不同导致出稿失败；完成一条任务后剩余任务变成新BGM批次。
- 根因: 配置未贯通实际写入器；用变化的查询快照替代持久批次状态。
- 以后怎么做: 写入与回读使用同一坐标；尚有同批任务时沿用一个普通批次ID，排空或完全换批后再创建。
- 证据: PR #22 独立代码审查、test_meeting_caption_integration.py、test_meeting_poll_fields.py。

### L-20261026-03 已知文案的 TTS 不应再由 ASR 重建字幕
- status: OPEN
- hits: 1
- 模块: audio-voice / validation
- 现象: 已知原文生成的 TTS 又经过识别，错字与模型依赖变成阻断出稿的主要原因。
- 根因: 把字幕时间需求扩大为重新识别内容，并用哈希账代替实际输出判断。
- 以后怎么做: 原文直接作为字幕正文，最终 TTS 真实时长决定每句区间；一句多镜统一字幕。检查实际媒体、窗口、文字和时间；不要求源码、提示词、拼图与收据逐层签名。
- 证据: docs/reviews/20261026-simplification.md、test_audio_captions.py。


### L-20261026-CACHE 共享素材缓存必须跨任务落盘并协调冷启动
- status: OPEN
- hits: 1
- 模块: visual-matching / runtime-and-recovery
- 现象: 同产品、同一组素材的不同任务重复执行视觉识别和文字复核。
- 根因: 默认路径跟随任务目录；并发任务各自看到缓存未命中；明确不可用的观察没有保存。
- 以后怎么做: 缓存落到队列公共目录或显式持久目录，任务信息不进观察键；同产品加锁后重新读取，完成结果再共享。否定观察可复用但保持不可用，失败和未完成复核不当成功缓存；兼容导入旧缓存。
- 证据: `test_shared_product_vision_cache.py`。

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
