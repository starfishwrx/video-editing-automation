# LESSONS.md — 错题本（Agent 写，人审）

规则：
- 每条教训一个条目；再次遇到同类问题时不新建，在原条目 `hits` +1 并追加证据链接。
- `hits >= 3` → 开 PR 升级为硬规则（AGENTS.md / docs/code-standards.md / 测试），条目状态改 `PROMOTED`，写明落点。
- 只写“下次怎么避免”，不写长篇过程。不写任何凭据、NAS 路径、业务表链接。

## 新增条目

### L-20261104-entry-preflight 入口预检必须覆盖恢复和真实调用路径
- status: OPEN
- hits: 1
- 模块: workflow/scheduled、batch_dispatch、orchestrator CLI
- 现象: 后台素材准备和恢复质检可能早于建稿环境检查；预检失败还可能消耗重试、覆盖旧选镜，异步提交则可能借用另一运行配置或重复执行。
- 以后怎么做: 用任务实际配置在付费步骤前预检；保留已有音频及选镜证据，预检不消耗选镜/建稿预算。忙碌时明确可重试阻断，旧异步提交保留并待核对；恢复写稿校验交付模式与匹配缓存一致。记录版本、策略与配置来源，不记录密钥。
- 证据: 调度、批次、恢复及无头预检反例；真实剪辑和生产安装须另行验收。

### L-20261104-runtime-diagnostic 交付依赖检查不能代替完整安装核对
- status: OPEN
- hits: 1
- 模块: tools/delivery_runtime
- 现象: 同一VERSION下，交付依赖文件一致但选镜或规则文件已漂移，固定交付文件列表无法证明整个安装面一致。
- 以后怎么做: 保留轻量默认检查；安装验收显式读取候选路径库存逐文件比对两执行面，拒绝缺文件、坏库存和路径逃逸。报告检查范围，外置后端、私有配置及额外文件另行核验；不凭旧ACTIVE或版本号宣称一致。
- 证据: test_delivery_runtime.py全库存漂移、缺文件、路径与CLI退出码行为回归。

### L-20261104-host-init 真实原生后端的用户初始化不可由冷启动替身证明
- status: OPEN
- hits: 1
- 模块: runtime/headless
- 现象: 新虚拟环境的资源与媒体冷启动通过，真实后端却因当前账号尚无剪映默认草稿索引而拒绝。
- 避免: 区分包依赖可安装与宿主已初始化；生产预检保留具体原因和同账号首次初始化步骤。只重分类已知初始化错误，不把其它后端故障混为一谈，不手工伪造首页索引。
- 证据: test_headless_host_initialization.py；docs/quickstart.md的首次使用说明。

### L-20261104-02 QC拒绝约束不能丢失像素缓存或绕回候选
- status: OPEN
- hits: 2
- 模块: selection / recovery
- 现象: 新增逐句QC拒绝源后整批观察缓存失效，短窗和跨句恢复还可能重复生成最终装配必拒的旧镜头。
- 根因: 可用性约束混入像素观察身份，且次级恢复入口未统一过滤拒绝源。
- 以后怎么做: 完整manifest继续绑定拒绝约束；观察缓存身份只绑定实际像素/语义输入。所有候选生产入口先按句排除拒绝源，共享池只为受影响句有界补充，最终装配保留独立硬闸。
- 补充: 拒绝集合必须在下次build覆盖报告之前原子累计保存，恢复按文案、商品、编辑输入及素材路径/大小/修改时间绑定。不能只从最后一次QC恢复；改稿或换源不能继承旧句号黑名单。补取未落checkpoint的QC时同时核对原input_manifest和源收据，不能只比两个旧报告的文案。
- 证据: `test_ai_editor.py`、`test_production_playback.py`、`test_sentence_continuity.py`；独立冻结fixture复审与全量回归。
- 恢复证据: `test_queue_contracts.py`覆盖累计A/B拒绝、QC后中断、后续报告覆盖、改稿换源、缺失来源与非法缓存。

### L-20261104-guard-origin 复核子窗与原始保护区必须保留不同边界
- status: OPEN
- hits: 1
- 模块: selection/ai_editor、application/engine
- 现象: 规划已扣除原始场景的头废料，复核窄窗却覆盖了scene起点，写稿再次扣头保护，造成证据起点恰好早300毫秒的伪越界。
- 以后怎么做: 保留实际复核场景边界，另带原始guard原点；验证包含关系后仅扣剩余保护，头尾默认值与求解器一致。回归要串起规划到engine请求并传入真实证据，另保留废料区和复核范围外证据的拒绝反例；恢复旧缓存重新assemble，不直接篡改匹配报告。
- 证据: test_ai_editor.py 的reviewed_subwindow正负例和test_paper_production.py既有连续取段覆盖。

### L-20261104-delivery-readback 写入确认必须对应实际业务字段
- status: OPEN
- hits: 2
- 模块: tools/workflow/closed_loop.table_update
- 现象: CLI返回成功不能证明全部状态已落入目标记录；旧首次成片值不在真实字段选项中。
- 以后怎么做: 用实际字段选项配置状态，成功后按record_id与字段名读回全部本次patch；容许有界可见性延迟。先保存写入ACK，恢复校验目标、记录、任务输入与patch身份后仅继续读回。原始提供方输出不得进入收据，防止凭据回显。一次成片不是人工验收。
- 补充: closed_loop/delivery_or_table_gate包含真实评估、分发与包损坏，延后通知必须绑定本轮具体table读回结果；未知消息不延后。
- 补充: 同目标记录发生任何中间状态写入前必须撤销delivery ACK，连未知写入结果也不可复用旧ACK。
- 补充: ACK后的可见性恢复不能被通用failure-update写成生成失败；仅同任务与目标的交付故障延后通知，真正生产失败仍通知。
- 补充: 截断日志只用于展示，不能再解析为恢复控制证据。finalization完整传递本次结构化table收据，worker保留结构化detail；通知延期必须与当前持久收据完全一致，长BGM字段不能导致误写失败状态。
- 证据: test_table_readback.py，覆盖漏字段、错状态、暂不可见、超时后只读恢复及旧ACK失效。
### L-20261104-01 集合语义的证据字段不能用序列顺序作一致性门禁
- status: OPEN
- hits: 1
- 模块: policy/continuity、selection/ai_editor
- 现象: 联合观察合法返回多个支持点，装配收据排序后却被成员门禁误报证据变化；单支持点夹具无法暴露问题。
- 以后怎么做: 明确字段是集合还是序列；仅对支持点ID按集合比较，保留原始观察，其他证据继续严格对齐。回归覆盖正反顺序、真实少点/多点、非法结构、装配和无付费复看的恢复，不仅调用单个校验函数。
- 证据: test_sentence_continuity.py 新增六项行为回归；docs/production-playback.md。

### L-20261103-16 修复入口和进展预算必须由程序掌握
- status: OPEN
- hits: 1
- 模块: selection/ai_editor、selection/sentence_continuity、selection/ai_editor_recovery
- 现象: 模型遵守时长约束而返回空选镜，反而进不了跨句修复；每轮只修一组时，四组合法连续演示被三轮文字纠错预算截断。
- 以后怎么做: 空选择先检查真实候选，区分证据缺失和有证据但时长无解。已复核长窗只是修复提案，必须重新通过装配和像素观察才能认证。局部修复取得进展后继续求解，与模型纠错轮次分离；逐句付费复核预算持久化，重复状态和总步骤均有限。测试多组、全部空选择、末组不确定及无进展情形，不只测一组成功。
- 证据: test_sentence_continuity.py 的7项复审回归；docs/production-playback.md。

### L-20261103-15 跨句预算不等于跨句证据成立
- status: OPEN
- hits: 1
- 模块: selection/sentence_continuity、policy/continuity、quality/continuity
- 现象: 两句分别求完整动作会无解；简单合并时长却可能借用无关卖点、同文件的另一实验，或让结果与口播阶段错位。
- 以后怎么做: 先短完整窗口，再对相邻同商品同卖点同演示进行联合实际画面复核；固定逐句配音字幕预算，只共享连续画面。逐句阶段是新的观察，原宽证据保留；所有组员、时序、源身份都要重验，不能单独复用一个片段冒称动作完整。写稿前按后端实际消费的源长度检查，写稿后独立解析活动文档；参数正确与观看质量分别验收。
- 证据: test_sentence_continuity.py；docs/production-playback.md。

### L-20261103-14 限速必须贯穿生产入口并区分观察与规划缓存
- status: OPEN
- hits: 1
- 模块: policy/playback、selection/ai_editor、selection/timing_contract、application/engine
- 现象: 实验有2/3倍规则，默认AI和旧成功计划仍可产生9–13倍；后台技术上限被误当成剪辑许可。
- 以后怎么做: 共享版本化生产政策，实际源窗/输出时长在写稿前重新计算；关键/结果及未知相位原速。旧成功标志和旧速度覆盖不构成许可，保留像素事实，仅重解或重选失败句；完整证据装不下报告冲突，不裁短证据伪造可行。实验配对、阶段与跨句预算必须先适配证据合同再接默认入口。
- 证据: test_production_playback.py；从冻结计划匿名提取的 production_speed_failures.json；docs/production-playback.md 中分别记录源码能力、未接入组件和部署状态。

### L-20261103-13 自动选曲回退必须区分坏素材和环境故障
- status: OPEN
- hits: 1
- 模块: audio/bgm_selector、audio/final_bgm
- 现象: 自动选中无效云曲后固定在失败收据；下载失败、损坏文件与工具或输出目录故障被混为一谈，并发恢复可能覆盖已成功使用的曲目。
- 以后怎么做: 自动选择限定候选、先准备和校验再确认；单任务锁保护整次选择到确认，已确认和显式曲目不自动更换。根据安全诊断区分坏音频与工具环境错误，保留尝试曲目；分别测试并发、损坏、空音轨、工具错误和正常回读。
- 证据: test_final_bgm.py 的真实损坏文件、并发锁与自动／显式选择反例；test_meeting_bgm.py 的收据一致性回归。

### L-20261103-12 最终媒体正规化必须保留选镜时钟和证据来源
- status: OPEN
- hits: 1
- 模块: audio/audio_loudness、application/recovery_audio、media/source_normalization、quality
- 现象: 原 48kHz 配音转 44.1kHz AAC 后与计划差几微秒，严格写稿拒绝；iPhone 显示矩阵旋转素材也被无头后端拒绝。仅放宽容差或删除旋转标签会掩盖真实缺音或改变显示画面。
- 以后怎么做: 最终旁白使用 48kHz PCM 的整数采样边界，检查实际 PCM 数据而不只读 WAV 头；处理缓存独立版本化、加锁并原子发布，不失效原 TTS。恢复保留生成来源并覆盖旧 file 绑定迁移。旋转通过完整无损工作副本解决，核对位深、解码像素、原声和帧 PTS；10bit x264 使用 qp=0，crf=0 并不保证逐像素相同。独立 QC 必须检查实际副本，同时绑定原证据和映射；另补空 profile 冷启动与真实缓存写出回读。
- 证据: test_headless_media.py、test_headless_media_coldstart.py、test_ai_editor_qc.py；四条隔离缓存恢复的本机验证收据另存，不随源码交付。
- 后续整合: #42 将新转正副本改为 v5 高画质限码率编码，兼容旧 v4 收据；无损全源副本会放大体积和并发磁盘 IO。压缩质量与证据时钟分别检查：保持分辨率/位深/原声/帧时钟和零偏移映射，允许副本像素压缩差异；跨句回读仍须核对实际副本内容和原素材绑定，不能按原文件字节直接拒绝合法转正，也不能放过换源。新增回归见 test_sentence_continuity.py。

### L-20261103-11 字幕居中、安全行宽与多行高度必须共同验证
- status: OPEN
- hits: 2
- 模块: editor/utils/subtitle_layout、media/timeline_render、application/headless_production、quality/draft_safety
- 现象: ASS关闭自动换行、原生文本默认左对齐时，长字幕会横向溢出。仅加行宽上限仍无法保证多行底部安全；位置上移后，旧固定坐标QC又会误报失败。高亮字号不同还会让基础字号比对误报。
- 以后怎么做: 字体测量与行数/描边共同计算安全框，默认水平居中；预览和原生写入共用策略，保留配音和时间。逐条回读实际布局和各高亮样式，插入换行重映射文字范围。无头后端能力必须在付费步骤前检查；源码结构、参数回读与原生像素验收分别报告。
- 后续纠正: 只平衡行宽仍会拆开产品名或动作结果。换行先保护词组与语义断点，再检查字宽；不要为了两行等长把“东方纸上抽纸”“四层加厚”“按压也不反渗”拆开。用新品上下文、数量单位、引导短语和动作结果等通用规则处理，并用未见过的产品名验证，不能只添加当前商品名特例。
- 证据: `docs/subtitle-layout.md`、`test_caption_layout.py`、`test_meeting_caption_integration.py`；包外真实字体9秒对照和原生待审草稿。

### L-20261103-10 品名修正要通过真实品类入口验证
- status: OPEN
- hits: 1
- 模块: rulepack、rules/zhipin
- 现象: 奶被纸写成奶杯纸，真实品名无法命中纸品默认规则；旧测试只比较显式覆盖值，没有检查商品识别到的品类。
- 以后怎么做: 业务名称和别名统一维护，验证实际品名经过apply_manifest_defaults后的品类与默认口播倍速，再检查显式覆盖仍生效；同时更新生产说明和行为记录，不用本机商品映射代替仓库规则。
- 证据: test_paper_rulepack.py 的奶被纸、tad厨房纸、TAD厨房纸与显式覆盖断言；PR #26。

### L-20261103-07 局部选镜失败应保留已完成观察与真实配音
- status: OPEN
- hits: 2
- 模块: selection/ai_editor、application/recovery_audio、workflow/selection_recovery
- 现象: 正式选镜仍强制整句卖点全覆盖；不适用品类标签使整次召回失败；换窗未补观察，服务超时又被包成不可重试的选错镜头。
- 以后怎么做: 共享至少一个具体卖点口径并如实记录未覆盖项，忽略不适用品类标签但保留审计；失败只改当前句，源窗变化重新看实际小窗。冻结真实配音、句长和任务输入，按原始运行持久记录一次阶段恢复，完成观察可复用、失败调用可补；中断不清预算、耗尽不回退全流水线。音频绑定同步匹配报告与草稿身份，选镜完成后超时不能使成功匹配失效；协议升级同时验证生产者和独立QC消费者。
- 补充: 切换局部与整稿重试时清除旧范围指令；重复源窗只用实际复看的候选求解，比较具体片段冲突并始终复验整稿。清单截断或空响应分批须逐批核对句子索引，历史超时与新增候选分别计预算。
- 证据: test_selection_recovery.py、test_selection_stage_resume.py、test_ai_editor_qc.py 的音频绑定、分批覆盖、多冲突与独立QC回归。

### L-20261103-06 运行映射表不能与私有缓存一起排除
- status: OPEN
- hits: 1
- 模块: audio/library_resources、application/cli、build_manifest、cloud_manager
- 现象: 本机有音色／音乐 CSV 而 Git 和源码包没有，旧测试只用内置音色导致干净拉取失败；缺表被误报为音色选择或无候选。
- 以后怎么做: 稳定名称／ID 元数据按字段白名单版本化，临时链接、音频和登录缓存另存；查询与下载共用同一索引。生产预检区分资源损坏和业务歧义，缓存命中前复查文件状态；合包拒绝缺少必需资源。用 Git 已跟踪文件及全新解压包，在隔离用户目录、禁网的新解释器验证真实名称、倍速和自动选曲链，不用本机安装目录补全。
- 证据: test_runtime_catalogs.py、test_runtime_resources.py、test_release_version.py；资源白名单和同类排查报告留包外。

### L-20261103-08 恢复阶段交接须校验真实产物与当前输入
- status: OPEN
- hits: 1
- 模块: application/cli
- 现象: 匹配生成裸manifest，写稿却要求完整成功结果；改读成功报告后，又可能消费本次匹配提前失败前留下的旧成功。
- 以后怎么做: 匹配入口共用完整报告合同，记录匹配开始前的原始任务输入；写稿仅消费成功、片段有效且输入一致的报告，恢复控制字段由当前阶段保留。所有匹配失败与异常都使旧成功失效，保留本次失败细节；不能把裸输入或无绑定旧报告补成通过。先串接成功两阶段，再验证提前失败、异常、输入变化与重复恢复均被拦住，不重跑配音或隐式全库识别。
- 证据: test_task_control.py 的实际匹配写出到恢复写稿串接及两入口失败反例；test_ai_editor.py 保持直接调用兼容。

### L-20261103-09 环境就绪要对应实际选择的写稿后端
- status: OPEN
- hits: 1
- 模块: runtime/headless、application/cli
- 现象: 旧工具链ready为true、CLI退出0，却没有可用无头后端或字幕字体；通用缓存还可能掩盖配置修复。
- 以后怎么做: 基础事实可以缓存，所选写稿后端即时复验；默认env汇总生产条件，base模式显式区分，任务后端和字体覆盖与build一致。返回实际ready和非零失败退出码，坏配置修复后可重新检查，不污染基础报告或要求云端调用。
- 证据: test_default_headless.py 的缺后端、基础模式、任务覆盖、字体失败后修复，以及付费前阻断回归。

### L-20261103-05 前置预检必须覆盖真实输入合同、异常与干净交付面
- status: OPEN
- hits: 1
- 模块: tools/batch_dispatch、audio/library_resources、build_manifest
- 现象: 仅捕获 VoiceResolutionError 漏掉倍速 ValueError，一个坏任务终止整批；只提交被发布库存排除的 CSV，开发目录查得到却无法交付，音乐表说明与提交不符。
- 以后怎么做: 与下游共用字段规范化和倍速验证，测试坏／好任务混批、修正恢复、原文回填、独立字段及 QC 恢复；预期输入和资源错误在本任务内记录。公开检索元数据与私有缓存分目录，打包缺表必须失败；新解释器只取跟踪／发布库存、空用户目录、禁联网验证真实名称与下游 ID，而非依靠宿主已有文件或源码存在性。
- 证据: `tests/test_batch_dispatch.py`、`tests/test_runtime_resources.py`、`tests/test_release_version.py`、`test_audio_catalog_admission.py`；PR #38 原 head `66c1e77` 与本轮修复。

### L-20261103-04 具体卖点的召回排序不能被通用展示池覆盖
- status: OPEN
- hits: 2
- 模块: vision/purpose_inventory、selection/purpose_retrieval、application/purpose_production
- 现象: 体验句被当成通用介绍，其专属候选被包装/本体展示池覆盖；后端即使能拒绝普通动作，也可能见不到有实际展示目的的素材。模型输出精确句长算术或省略式引用又会使整次召回失败。
- 以后怎么做: 按当前原句与至少一项具体卖点召回，描述只作带出处的线索，最终源窗重新看实际画面；展示句终点由程序计算。丢弃并记录无效单条线索，保留有效候选，先三条失败最多补三条。仅普通介绍允许同商品池分散，具体卖点不能靠通用补池通过。匿名ID复验、反例淘汰和跨案例分别记录，不用单个成功泛化整稿稳定。
- 补充: 无卖点的开头钩子虽然走规则召回，也要先按包装目的排序再截前6个；生产层二次分配不能因去重预留把包装重新挤掉。测试“8个动作在前、唯一包装在后”并反序/换匿名ID，确认两阶段仍能送包装候选进入实际复核；中英描述、否定包装与背景包装分别处理，不把关键词命中当成视觉通过。
- 证据: `test_purpose_retrieval.py`、`test_shot_usability.py`、`docs/shot-selling-point-value.md`；卫品42自动召回、匿名ID及萌睡裤定向复验保存在包外。

### L-20261103-03 同商品与手部动作不能替代有效的信息展示
- status: OPEN
- hits: 2
- 模块: vision/shot_quality_review、selection/shot_usability、application/display_optimization
- 现象: 给模型文案后，翻折整理被解释成薄软、舒适；单纯要求无文案复核时又可能过度要求实验，误拒正常手持展示。
- 以后怎么做: 先不带目标观察实际主体/信息可读区间，再作语义对应；稳定展示、少量轻触与遮挡收拢分开。盲区间只作上限，与目标特征可读区间取交集，保留原答，不能把产品轮廓清楚扩张成某一卖点整段清楚。可读段原速，最终输出源窗比选，置信度靠后；有明显整理或未完成结果则不强塞。换源前核对观察身份，协议变化使完整旧产物失效，旧索引只召回。代码可用与实际功效、整片验收分开。
- 证据: `test_shot_usability.py`、`test_purpose_production.py`、`docs/shot-usability.md`；卫品42第7句实际对照及失败记录留包外。
- 补充: 先判断动作/展示是否传达当前原文卖点，再看可读性。至少一个具体卖点即可，未匹配其他项只备注，不把部分支持当淘汰条件；不能用“更清楚”证明卖点提升，也不能以多个间接关联压过一个直接演示。新合同与033/036反例、076相关表达见`docs/shot-selling-point-value.md`。

### L-20261103-02 观察动作事实与编辑倍速分开，实验组能放下不等于逐句对齐
- status: OPEN
- hits: 1
- 模块: vision/experiment_event_review、selection/experiment_events
- 现象: 视频观察将准备阶段标成按压，吸收又被一概锁原速；共享预算虽能放下，反渗句仍播放倒液。
- 以后怎么做: 模型先给实际接触/提起/可读结果编号，密集原帧校正，程序再执行允许的局部加速与关键节点保护。吸收过程也可加速，计时/瞬吸主张则整段禁速。固定句长分别约束相位，无解明确记录；定性吸收可显式选连续倒液尾段，报告省略的前段，不能将此说成完整倒液。按原生PTS核算末态保护，不把3个10fps采样误当300ms。输出模型抽样误读两侧相同时保留失败，实际逐时点帧对照另行核验，不能只引用有利评分。
- 证据: `test_experiment_events.py`、`docs/weipin-experiments.md`；卫品42实际2倍过程、反渗句对应及独立帧复核记录留包外。

### L-20261103-01 阶段规格不能替旧观察绑定新素材
- status: OPEN
- hits: 1
- 模块: application/experiment_optimization
- 现象: 规格只记录来源路径时，同名文件替换并重新配对后，旧帧编号可能被错误绑定到新媒体。
- 以后怎么做: 用正常命令从当前配对生成带原输入身份的待复核模板，规格复用必须核对包含有序来源、采样映射、复核窗口及规则的既有配对输入身份。媒体、映射或窗口改变后明确拒绝；不能为旧规格自动生成新绑定。空复核说明不可直接执行；记录自动观察与定向复核的来源区别。
- 证据: `test_experiment_optimization.py`的同路径替换、采样映射与窗口变化反例，以及`phase-template → optimize`正常入口回归。

### L-20261102-10 原速冲突不代表允许局部加速也无解
- status: OPEN
- hits: 1
- 模块: application/experiment_production、selection/experiment_retiming
- 现象: 用户允许局部加速，新入口却把整个实验窗口保护为原速，随后把时长冲突作为验证结果。
- 以后怎么做: 按实际动作分开首尾、过程和结果，关键区间原速，组内共享预算先≤2倍再≤3倍；保留连续源区间，优先选一次完整验证，省去之前重复演示。原速、允许变速下可行性及实际成片质量分别记录。模型边界不稳定时保留分歧，人工或主线程定向帧校正明确标注，不假称全自动。源码测试不替代输出视频复核。
- 证据: `test_experiment_retiming.py`、`docs/weipin-experiments.md`；计划2.94倍、整数帧实际过程3.0/2.4倍、25帧纸巾结果及独立对照复核留包外。必须检查实际采样跨段边界，不凭计划倍率宣称执行上限成立。

### L-20261102-09 同一次实验不能收窄为同一个文件
- status: OPEN
- hits: 1
- 模块: selection/experiment_contract、vision/experiment_pair_review、application/experiment_production
- 现象: 把“按压不能换实验”解释成禁止跨文件，错误排除相邻拍摄的倒液与纸巾验证。
- 以后怎么做: 同次实验可跨文件，相邻序号只作召回；联合核对商品、承载物、巾体状态及验证关系，保留非相邻正例与相邻错配反例。复核输入绑定有序源文件、窗口、实际帧和图片摘要，换输入必须重查。配对、时长和最终成片分别验收；041单文件缺阶段不能推成整组缺实验。
- 证据: `test_experiment_pairs.py`、`docs/weipin-experiments.md`；真实配对请求与画面留包外。此澄清覆盖下方L-20261102-08中的“必须同源”解释。

### L-20261102-08 新入口须逐条接入已有品类规则，技术通过不能替代业务验收
- status: OPEN
- hits: 1
- 模块: ai_editor_contract、purpose_production、experiment_production
- 现象: 新实验借透气主题时间，模型匹配数上升后误报改善；早期又将实验对应要求误解成必须单文件，已由L-20261102-09更正。
- 根因: 品类规则注入仅覆盖纸品；新组装只检查时长、倍速和重复，缺少业务条件；审阅与测试范围也未逐条对应原规则。
- 以后怎么做: 新旧入口共享当前品类合同，复用旧描述时重新编译原文，不沿用旧支持结论。完整实验必须同一次、阶段齐全且主题兼容，允许画面已确认对应的跨文件组合；原件/代理关系由受控准备保存，未知来源停止。增加旧入口、跨实验错配、跨主题、错序、漏结果、错商品与输出覆盖反例。定向补查只恢复实际看到的部分事实，不能拼回答获得完整实验认证。未得到更优合格成片时明确报告未提升。
- 证据: `test_weipin_experiment.py`、`test_temporal_samples.py`、`docs/weipin-experiments.md`；真实卫品验证与独立审阅收据留包外。

### L-20261102-12 视频输入不等于可靠时间定位，短句可共享完整视觉演示
- status: OPEN
- hits: 1
- 模块: vision/temporal_video、selection/temporal_groups
- 现象: 服务能识别原生视频的颜色切换，却对短真实视频返回越界时间；逐句完整实验又因固定短口播无法容纳。
- 根因: 视频理解仍是抽样且时间定位会漂移；把每句都当独立剪辑单元，会重复占用准备、动作与结果预算。合并保护区也可能意外丢掉结果的最少帧数。
- 以后怎么做: 实测服务能力，保存真实视频输入与FPS身份，越界时间直接拒绝；按真实PTS帧复核拟用源窗。允许连续相邻句共享真实动作链，关键动作及结果保留原速，结果独立保留帧数约束。组外画面、音频字幕和非视觉未解决项不能被组装覆盖。功能补齐后再看整片卖点关联，不能只拿模型匹配计数宣布整体提升。
- 证据: `test_temporal_video_groups.py`、`docs/temporal-shot-groups.md`；真实媒体与请求账本留包外。

### L-20261102-11 目的性识别须保留反证与完整原文复核
- status: OPEN
- hits: 1
- 模块: vision/purpose_review、selection/purpose_selection、application/purpose_production
- 现象: 粗召回能把包装或固体道具误列为实验；需求编译也可能把修辞变成证明任务，或漏掉持续时间断言。
- 根因: 文案会诱导观察，模型生成的需求和时间线索并非事实。将编译器的用途解释交给复核，会继续传递偏见；只看音频边界还可能把邻句一帧当作本句证据。
- 以后怎么做: 只复用同任务素材白名单与有效缓存；实际窗口重新看图，观察不接收目标句长；体验/展示沿用业务用途，明确性能保留动作和结果。最终复核独立读全部原文，不接收编译器分类解释，能报告遗漏；按实际分配的画面帧界限抽样，诊断占位必须有结构否决。修复预留所有仍使用的源窗，覆盖漏句、输入变化和反复处理旧目录等反例。抽样支持不等于正式验收。
- 证据: `test_purpose_production.py` 与目的性识别方案；真实输入与结果仅保存在隔离验证目录。

### L-20261102-07 按改动范围测试，避免重复全量
- status: OPEN
- hits: 1
- 模块: 测试策略、AGENTS、包级与字幕回归
- 现象: 把每次小修改都当发版，在开发树、解压包、安装面反复跑全量，拖长迭代；源码字符串和文档数字断言还会因无关格式变更失败。
- 以后怎么做: 小改动先跑对应模块与直接调用方；共享合同／正式门禁变化和范围不确定时全量，发版保留最终解压验收，安装只补入口与案例。删除已有行为覆盖的重复检查，取消无业务价值的静态文字约束；不同的音频、证据、formal和恢复失败边界不能为减少数量而删。
- 证据: 35项BGM／默认无头定向验证约7秒，完整历史源码回归约136秒；5项静态／重复检查移除后，保留的入口及字幕测试读取实际执行结果，独立审阅通过。

### L-20261102-06 默认后端切换要贯穿读稿与交付
- status: OPEN
- hits: 1
- 模块: application/headless_production、runtime/draft_documents、tools/native_delivery
- 现象: 原生稿已建出，但旧QC、交付和版本归档只读明文；新副本codec也可能因目标文件已存在而拒绝操作。
- 以后怎么做: 默认入口、付费前预检、当前镜像读取、字体资源、独立QC、分发及恢复一起回归。编码写入新临时密文，再替换唯一新副本；六镜像和活动指针逐一核验。音频时长使用最终音频探针，不能套视频首帧修正。用真实复制、已存在目标、单侧中断、版本重命名以及内容漂移反例验证，保留正式质量门禁和源稿。
- 证据: 默认入口测试、卫品固定缓存回归、原生NAS传输／版本恢复收据与独立代码审阅；文件技术成功不替代商品内容验收。

### L-20261102-05 业务空BGM必须进入准备阶段且核验有效源长
- status: OPEN
- hits: 1
- 模块: audio/final_bgm、application/production、tools/production
- 现象: 固定输入测试将空BGM当成不添加，无头导出继续保留无音乐时间线；短音乐也可被APAD补成长静音后仍通过整体LUFS检查。
- 根因: 技术时间线入口绕过worker空值规范化，响度门槛没有证明全片都有有效音乐。
- 以后怎么做: 业务命令必须有任务输入，统一空值自动选曲和重试/批次选择记录；显式ID/标题/路径保持优先。按实际选用音频源长检查去静音和起点后的完整窗口，拒绝短源与含视频的歧义源，不将静音补齐当成成功。淡变仅处理一次，BGM独立轨，旁白/字幕/视频时序和字节按本次修订回读；缺音乐明确阻断。
- 证据: `test_final_bgm.py` 的空值合同、默认CLI必填、短源/前置静音、带视频短音频、批次别名、真实PCM/LUFS及原编辑决定保留；独立复验与真实交付收据分别留证。

### L-20261102-04 无头草稿交接应验证当前格式和实际路径
- status: OPEN
- hits: 1
- 模块: delivery/jianying_export、quality/timeline_export、tools/headless_review_delivery
- 现象: 明文 writer 对最终音频按毫秒截短；切换原生加密后，旧 NAS 工具无法读取实际活动时间轴，字幕粗体也可能被静默丢掉。
- 根因: 不同底座的媒体时长、文字参数和草稿格式有不同表达，结构成功不代表参数已经完整映射。
- 以后怎么做: 固定最终时间线，用外部后端的正式计划与 codec API 构建；逐微秒回读时序，显式映射并核对粗体、源窗、附加速度材料和字面媒体路径。NAS 收集包括字体，复制到新目录后再读实际加密镜像；拒绝设备路径和报告/草稿目录重叠。不得用旧明文替代、放宽成片认证或把参数验证当成观看通过。
- 证据: `test_timeline_export.py`、后端 detached handoff 单元测试及本次真实待审构建/共享路径回读；支持范围见 `docs/programmatic-production.md`。

### L-20261102-03 精确选帧表达式必须覆盖真实连续动作长度
- status: OPEN
- hits: 1
- 模块: media/timeline_render / quality/production_qc
- 现象: 一秒合成片可渲染，真实多句连续动作的 188 项平铺选帧表达式却报 Cannot allocate memory，GPU/CPU 编码器均失败。
- 根因: FFmpeg 表达式解析有递归深度限制，平铺求和在尚未触及命令长度预算时已超限；不是换编码器或加内存可以解决。
- 以后怎么做: 对同一精确索引集合生成平衡表达式，不改成平均 FPS 或删帧规避。回归包含长连续事件、保护末帧和实际像素对应；按结构、内容分别记录失败，内容证据不足不能混记成媒体损坏，也不得借此放行。
- 证据: `test_timeline_render.py::test_long_continuous_action_does_not_exceed_expression_parser_depth`、内容缺口及非法类型回归；独立复核 1–1000 项中的 12 种规模逐字节符合预期。

### L-20261102-02 时间线末端与保护区必须核验真实输出帧
- status: OPEN
- hits: 2
- 模块: domain/timeline / media/timeline_render
- 现象: 非整帧配音若把整片帧数直接 floor，会有末尾旁白没有画面；名义源窗覆盖结果，也可能采不到所需结果帧。Matroska 的 60fps 还会因 1ms time_base 表现为 16/17ms 交替。
- 根因: 业务微秒、容器时间基和输出帧预算混用，或仅凭 avg_frame_rate 判断源时间分布。
- 以后怎么做: 内部切点共享累计 floor、整片末端 ceil，最后一帧必须有真实源画面；实际 PTS 在容器精度内核对 CFR，再核验选中源帧和保护区的输出帧数量。短源、缺帧、编码增删帧保持阻断，不靠尾帧克隆或截旁白补齐。
- 证据: `test_edit_timeline.py`、`test_timeline_render.py` 的非整帧、非零 PTS、逐帧像素及保护结果反例；实现边界见 `docs/programmatic-production.md`。
- 本轮追加: 手机旋转/VFR源必须显式派生为保比例、固定输出帧率的资产；普通展示入点对齐真实PTS时，依据实际观察的关键相位判断保护，不能只看请求布尔值。显式诊断尾量仅允许最后一个输出帧使用下一张真实源画面，并逐帧记账，formal和默认路径不变，双尾帧/缺源仍拒绝。新增反例见 `test_purpose_production.py`。

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
