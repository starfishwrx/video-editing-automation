# 卫品吸水实验：同一次实验允许跨文件

倒液吸收与后续纸巾按压、提起及结果必须对应同一次实验，允许分别拍在多个源文件中。不能用另一轮实验的验证替换。此前将这一要求收窄为“必须同一个文件”是实现方误解，已按用户澄清更正。

相邻序号如31/32、IMG_3431/IMG_3432只用于优先召回，不证明实验对应；不相邻的文件也可以通过真实画面配对。透气、不闷热、荧光、商品切换及促销主题不并入吸水组。

默认 `ai_editor` 已接入同次实验配对、事件定位与局部变速。下文的 `tools.weipin_experiment` 仍是显式预览工具；默认生产直接生成现有逐句/多镜 manifest，沿用原生建稿、独立 QC 和正式交付门禁。源码接入不代表已安装或真实成片已验收。

## 默认三种处理如何协作

1. 单源分相保留：按句子语义把吸收、纸巾验证分配到已定位的源阶段，必须经过实际窗口及完整连续组复核。历史计划仍可用旧关键词作召回线索。
2. 同源原速连续句组保留：相邻口播共同承接同次演示时，优先利用现有 `sentence_continuity`，不要求每句重播完整动作。
3. 前两种尚未覆盖的吸收/反渗组进入 `selection/experiment_groups.py`：对候选进行实际源帧配对，再观察事件、密集细化边界，按固定配音求解局部变速。单文件也可以经此路径分开定位倒液吸收与按压验证。完整倒液无解时，仅合规的定性吸收口播可尝试已观察的倒液尾段，省略前段如实写入组内 `source_selection`。

三种策略共用 `policy/continuity.py` 的相邻句、源顺序和完整组合同。实验预览与默认生产共用 `selection/experiment_contract.py::validate_pair_evidence`、事件编译和阶段求解，不复制一套正式验收。普通完整短窗仍允许最高2倍；实验分相的接触、完成、提起、结果原速，可压缩过程优先≤2、最多3倍。

用途识别在正常文案 inventory 中给出 `experiment_role`；透气、荧光、促销及商品切换不并入吸水组。组尚未认证时不能靠几条分别相关的镜头放行。每组的不同候选配对上限以`selection/experiment_groups.py::PAIR_LIMIT`为准，当前为6，优先覆盖不同来源；它计候选镜头组合，不是最终实验数量或模型调用数。同一作用域预算随任务恢复，耗尽只报未确认，不能宣布全库缺素材。计数与同源连续组预算的区别见[AI编辑合同](../packages/video-v1.5.3/docs/AI_EDITOR.md#执行流程)。不清空配音或素材事实重跑全库。

默认事件观察使用现有 GLM/Qwen 图片客户端的实际编号帧与密集边界复看，不要求另配 Qwen 视频服务。粗观察窗口可在边界处重叠，事件细化后的实际选段仍必须有序、无重播，不能盲裁重叠区。

新组采用 `reviewed-experiment-sentence-group-v1`，携带每句、每个恒速物理片段及其实际源窗。写稿前重验真实音频和原件身份，写稿后回读源/目标时长、速度、素材及转场；独立 QC 同批看完整组，只拿原文和实际输出抽帧，不拿选镜器通过结论。局部 QC 修复重验整组关系，已接受邻句的实际切片锁定，组外不变；无法在这些限制内修复时停止，不回退成独立句拼接。

## 事件边界与逐句相位优化

`events`先用实际编号视频观察事实，再用边界附近的密集原帧校正。短窗口逐个检查10fps编号帧；B另给首次纸巾接触、开始提起、结果可读三个编号，准备不能算成按压。模型只报事实，程序保护首尾、提起和结果，中间倒液、吸收及按压过程允许先≤2倍、必要时≤3倍。吸收末态至少保留300ms原速；纸巾结果至少15个实际输出帧。10fps观察仍有约100ms采样边界，不能宣称源帧级识别已经稳定。

```powershell
uv run --frozen python -X utf8 -m tools.weipin_experiment events --manifest work/task.json --evidence work/pair.pair.json --focus absorption_focus --font C:/Windows/Fonts/arial.ttf --output work/events.json
uv run --frozen python -X utf8 -m tools.weipin_experiment optimize --manifest work/task.json --timeline work/fixed/timeline.json --roles work/roles.json --evidence work/pair.pair.json --motion work/events.motion.json --prior-origins work/origin-bindings.json --align-phases --report-dir work/aligned-new
```

`--align-phases`从反渗句起点分开求解两份源窗预算，按压准备自动去掉，纸巾实际接触对上该句；放不下明确报`EXPERIMENT_PHASE_DURATION_CONFLICT`，不再仅凭实验组总长宣称逐句已对齐。未启用时保留旧的整组连续分配行为，仍输出相位偏移。

`--focus complete_pour`为观察默认值，保留完整倒液。显式`absorption_focus`只用于定性吸收卖点：允许从已观察到的倒液尾段取连续窗口，至少保留400ms液体流入，后续吸收和可读末态均保留；优先选择2倍以内能放下的最早源起点，再尝试3倍。省略的倒液前段写入`source_selection`和`changes.json`，没有内部切过程或补静帧，也不冒称展示完整倒液。含倒液完整性、毫升数或计时主张时不能用该覆盖；含计时/瞬吸主张时两种模式都禁止加速倒液和吸收过程。

卫品42本轮只复查041/042，原13句及直率英子1.2配音保留。吸收选窗5.600–7.816667秒，实际两个过程段均2倍，接触/完成/末态原速；按压验证0.900–3.816667秒，中间过程实际约1.161倍。时间线按压起点偏移0，30fps实际首帧早29.751ms，边界识别仍受上述采样误差限制。纸巾结果37帧，仍延续到体验总结句，不能称整条反渗证据已全部放进0.998027秒的单句。

A使用此前用户看过的完整倒液局部加速预览，B为本轮吸收卖点取段；不是与诊断黑场比较。固定18份音频/字幕文件字节一致，组外源帧/PTS映射一致；最终H264重编码令部分组外重建像素不同，不宣称整片逐像素相同。冷读实际帧对照支持B在反渗口播期已有纸巾接触，结果也较早可读；独立MP4模型复核却误称两侧相同及B为完整倒液，该答复保留但未作验收依据。局部对应改善不代表已证明整体达到人工示例质量，播放节奏与正式交付仍待验收。未部署、未回写。

## 用正常命令复现已复核方案

已确认的配对、句子用途、源关系和阶段数据可直接交给程序，不再编写任务专属Python脚本。已有绑定motion时运行：

```powershell
uv run --frozen python -X utf8 -m tools.weipin_experiment optimize --manifest work/task.json --timeline work/fixed/timeline.json --roles work/roles.json --evidence work/pair.pair.json --motion work/verified.motion.json --prior-origins work/origin-bindings.json --report-dir work/optimization-new
```

需要定向修正自动观察的阶段时，先生成可编辑规格：

```powershell
uv run --frozen python -X utf8 -m tools.weipin_experiment phase-template --manifest work/task.json --evidence work/pair.pair.json --motion work/observed.motion.json --output work/phase-spec.json
uv run --frozen python -X utf8 -m tools.weipin_experiment optimize --manifest work/task.json --timeline work/fixed/timeline.json --roles work/roles.json --evidence work/pair.pair.json --phase-spec work/phase-spec.json --prior-origins work/origin-bindings.json --report-dir work/optimization-new
```

两条命令之间依据实际编号帧核对并修改`members[].observation`，在`review_note`记录复核依据；空说明的模板不能直接使用。`phase-template`不带`--motion`时生成待填写的uncertain观察，不会假造已识别阶段。规格中的来源、复核窗口及`pair_input_sha256`由命令填写，不能挪作别的输入；同名换媒体、重新采样或窗口变化后旧规格拒绝复用。

`optimize`只读取现有输入并在新目录输出`timeline.json`、绑定后的`motion.json`、逐句源窗/倍速变化表`changes.json`和`REPORT.md`、`render/preview.mp4`及`result.json`。它不重做配音、音乐或素材分析；输入已有音轨及组外视频不变。结果同时给出计划倍率、实际输出帧预算倍率、保护区帧数和反渗句与按压起点的滞后，不能把一个组能放下当作逐句已对齐。

`--motion`与`--phase-spec`二选一。存在多个合法句组时用重复的`--sentence-id`明确选一个；默认只处理唯一句组。`--no-render`仅准备时间线和变化表，默认会实际渲染。输出目录已存在时拒绝覆盖，阶段失败保留`BLOCKED`结果。该能力不会自动回退重跑全库，不获得正式交付权限。

## 召回与画面配对

`weipin.json`的`experiment_contracts.absorb_rewet`声明同次实验、可跨文件和结果最低帧数。有效规则同时进入新旧文案识别；旧描述只作召回，原文按当前规则重新编译，旧的支持结论不会自动获得当前认证。

`purpose_production.select`分别召回吸收及反渗候选，输出`experiment_pair_candidates`。同目录相邻序号优先，其他候选仍保留；相邻标记的`acceptance_evidence`始终为false。

媒体准备保存原件→CFR60代理→实际编号帧链路，每6帧取一帧。已有物理缓存在原件、代理、编号视频及映射均未改变时可复用，不复用旧的单文件完整性结论作配对证据，也不自动重识别全库。

`pair`联合查看两份候选的实际编号画面，每份源窗口取8帧。观察不接收文件名、邻接标记、旧结论或目标句长；逐项核对商品、实验承载物与布置、巾体湿润状态、按压验证对应关系，并分别记录倒液/吸收和按压/提起/结果的可见帧。冲突为mismatch，不能确认则uncertain，只有阶段齐全且四项对应均支持才为matched。这是抽样画面支持，不是连续录像证明或最终成片认证。

复核提交前落盘独立输入绑定，包括有序成员、完整采样映射摘要、源窗口、实际可见帧和图片SHA256，绑定摘要进入模型请求。组装重新校验绑定及媒体身份；交换成员、同步修改窗口和可见帧列表、替换图片均须重新复核，不能套用旧matched。输入输出碰撞在请求前拒绝。

## 固定时间线组装

同文件完整实验和已确认的跨文件配对均可进入组装。源切换需要已确认的有向实验配对；文件相邻不能获得切换许可。检查原文、商品、规则与任务身份、阶段顺序、结果可读帧、源边界及同源重播，保留配音、句长、字幕、BGM和组外画面。

组外已有的诊断黑场可以随预览保留，不要求它伪装成任务原素材。只认可明确的占位标记、对应未解决项及同一占位资产所有使用一致的情况；其未解状态不清除，真实素材的来源和重播检查继续执行。单文件与跨文件两条入口采用同样处理。

配对后可通过`motion`按实际编号视频观察有效窗口及动作阶段，再用`apply --motion`启用局部变速。开始接触、完成、提起和结果保护为1倍；明确可压缩的中间过程共享整组预算，先求解2倍以内，无解再尝试最多3倍。保留选定连续源窗的全部区间，不靠裁过程或补静帧凑时长。新结果窗口至少15个实际输出帧，仍需观看可读性检查。

未提供有效motion证据的入口保留原速保护；提供motion后无解明确报3倍上限冲突，不静默回退或超速。单文件完整实验目前仍走旧原速封装，新的局部变速验证范围为跨文件实验配对。上一版把整个窗口锁原速只是未完成的实现，不能据此宣布用户允许的局部变速方案无解。

## 显式脚本入口

在活包根目录运行，凭据沿用已有`JY_QWEN_*`进程配置。`observe`要求`JY_VISION_TRANSPORT=qwen`并使用编号视频；`pair`通过已有视觉客户端提交联合真实帧图片。

```powershell
uv run --frozen python -m tools.weipin_experiment roles --manifest work/task.json --timeline work/fixed/timeline.json --output work/roles.json
uv run --frozen python -m tools.weipin_experiment observe --manifest work/task.json --source work/pour.mp4 --report-dir work/pour-new --font C:/Windows/Fonts/arial.ttf
uv run --frozen python -m tools.weipin_experiment observe --manifest work/task.json --source work/press.mp4 --report-dir work/press-new --font C:/Windows/Fonts/arial.ttf
uv run --frozen python -m tools.weipin_experiment pair --manifest work/task.json --first-evidence work/pour-new/observation.evidence.json --second-evidence work/press-new/observation.evidence.json --first-window 20 78 --second-window 5 38 --output work/pair-new.json --font C:/Windows/Fonts/arial.ttf
uv run --frozen python -m tools.weipin_experiment motion --manifest work/task.json --evidence work/pair-new.pair.json --output work/motion-new.json
uv run --frozen python -m tools.weipin_experiment apply --manifest work/task.json --timeline work/fixed/timeline.json --roles work/roles.json --evidence work/pair-new.pair.json --motion work/motion-new.motion.json --prior-origins work/fixed/origin-bindings.json --output work/timeline-new.json --render-dir work/render-new
```

窗口参数是对应采样缓存的本地frame编号，不是秒数。`prior-origins`由已有受控来源收据生成：每项含`asset_id`、原件/代理的实际`path/size/mtime_ns`及`clock: same_source_clock`，不能按文件名猜来源。新目录存输出，该入口不写NAS、业务表或生产队列。

## 卫品42定向验证

固定原13句、直率英子1.2倍配音及现有音频。吸水合法组为第8–10句，共4.163719秒；第7句不闷热仍不可借用。

联合画面复核支持3431/3432、3513/3514、3513/3515配对，最后一组序号距离为2；3431/3514因木桌平铺与软垫承载场景冲突被拒绝。这是三个候选组合、涉及两个倒液场景，不应计为三次独立实验。041本文件没有按压，不代表041/042这一组缺少验证。

此前三个matched组合只按原速保护测试，时长冲突不能代表局部加速也无解。后续对041/042重新检查开始帧并扩大配对观察范围，用一次完整按压代替文件内重复按压；保留原13句、字幕、BGM，在4.163719秒内实际渲染局部加速候选。计划倍率约2.94，实际输出按整数帧预算为倒液过程3倍、移杯过程2.4倍，纸巾结果25个实际输出帧。原声/背景音乐/混音/字幕四份派生文件与上轮字节一致。

过程片段显式携带`bounded_process_speed_cap`，渲染按真实分配的输出帧均匀覆盖源窗，防止压缩段尾接原速时形成过大的采样跳步；实际输出帧预算超过3倍就拒绝。最终同源相邻采样最大6个60fps源帧，对应30fps输出不超过3倍。该规则只对明确标记的可压缩过程生效，保护区原速和既有路径不变。

本次边界经过主线程实际编号帧定向校正：保留模型意见分歧，不能称为全自动稳定选镜。自动视频观察曾返回整段按压可加速（校验拒绝），另一次又将整段倒液锁原速。最终候选明确标记`main_thread_actual_frame_editorial_candidate`，单独做输出复核；原模型答复不改写。

独立实际对照视频抽样支持新片段动作完整、结果可读和节奏；对照A是上轮固定预览（前半有诊断黑场），不是人工示例或原生产稿。它证明目标片段相较该失败预览有改善，不证明全片优于原选择器。结构媒体PASS，内容仍保留待输出复核项，整片观看和正式交付未通过。真实媒体、请求与证据留包外，未部署、未回写。
