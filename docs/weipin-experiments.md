# 卫品吸水实验：同一次实验允许跨文件

倒液吸收与后续纸巾按压、提起及结果必须对应同一次实验，允许分别拍在多个源文件中。不能用另一轮实验的验证替换。此前将这一要求收窄为“必须同一个文件”是实现方误解，已按用户澄清更正。

相邻序号如31/32、IMG_3431/IMG_3432只用于优先召回，不证明实验对应；不相邻的文件也可以通过真实画面配对。透气、不闷热、荧光、商品切换及促销主题不并入吸水组。

当前仍为显式预览入口，未切换默认生产。素材关系、固定句长可行性、最终观看质量分别验收。

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
