# 卫品吸水实验：同一次实验允许跨文件

倒液吸收与后续纸巾按压、提起及结果必须对应同一次实验，允许分别拍在多个源文件中。不能用另一轮实验的验证替换。此前将这一要求收窄为“必须同一个文件”是实现方误解，已按用户澄清更正。

相邻序号如31/32、IMG_3431/IMG_3432只用于优先召回，不证明实验对应；不相邻的文件也可以通过真实画面配对。透气、不闷热、荧光、商品切换及促销主题不并入吸水组。

当前仍为显式预览入口，未切换默认生产。素材关系、固定句长可行性、最终观看质量分别验收。

## 召回与画面配对

`weipin.json`的`experiment_contracts.absorb_rewet`声明同次实验、可跨文件和结果最低帧数。有效规则同时进入新旧文案识别；旧描述只作召回，原文按当前规则重新编译，旧的支持结论不会自动获得当前认证。

`purpose_production.select`分别召回吸收及反渗候选，输出`experiment_pair_candidates`。同目录相邻序号优先，其他候选仍保留；相邻标记的`acceptance_evidence`始终为false。

媒体准备保存原件→CFR60代理→实际编号帧链路，每6帧取一帧。已有物理缓存在原件、代理、编号视频及映射均未改变时可复用，不复用旧的单文件完整性结论作配对证据，也不自动重识别全库。

`pair`联合查看两份候选的实际编号画面，每份源窗口取8帧。观察不接收文件名、邻接标记、旧结论或目标句长；逐项核对商品、实验承载物与布置、巾体湿润状态、按压验证对应关系，并分别记录倒液/吸收和按压/提起/结果的可见帧。冲突为mismatch，不能确认则uncertain，只有阶段齐全且四项对应均支持才为matched。这是抽样画面支持，不是连续录像证明或最终成片认证。

复核提交前落盘独立输入绑定，包括有序成员、完整采样映射摘要、源窗口、实际可见帧和图片SHA256，绑定摘要进入模型请求。组装重新校验绑定及媒体身份；交换成员、同步修改窗口和可见帧列表、替换图片均须重新复核，不能套用旧matched。输入输出碰撞在请求前拒绝。

## 固定时间线组装

同文件完整实验和已确认的跨文件配对均可进入组装。源切换需要已确认的有向实验配对；文件相邻不能获得切换许可。检查原文、商品、规则与任务身份、阶段顺序、结果可读帧、源边界及同源重播，保留配音、句长、字幕、BGM和组外画面。

当前业务组装器保护必要源窗为1倍速。通用求解器原有2/3倍能力保留，本入口尚未增加可压缩过程的自动规划。配对通过仍可能报时长冲突；不能借其他主题的句长、删必要动作、补静帧或暗中加速获得通过。结果最低15个输出帧只是防退化下限，仍需真实观看复核。

## 显式脚本入口

在活包根目录运行，凭据沿用已有`JY_QWEN_*`进程配置。`observe`要求`JY_VISION_TRANSPORT=qwen`并使用编号视频；`pair`通过已有视觉客户端提交联合真实帧图片。

```powershell
uv run --frozen python -m tools.weipin_experiment roles --manifest work/task.json --timeline work/fixed/timeline.json --output work/roles.json
uv run --frozen python -m tools.weipin_experiment observe --manifest work/task.json --source work/pour.mp4 --report-dir work/pour-new --font C:/Windows/Fonts/arial.ttf
uv run --frozen python -m tools.weipin_experiment observe --manifest work/task.json --source work/press.mp4 --report-dir work/press-new --font C:/Windows/Fonts/arial.ttf
uv run --frozen python -m tools.weipin_experiment pair --manifest work/task.json --first-evidence work/pour-new/observation.evidence.json --second-evidence work/press-new/observation.evidence.json --first-window 20 78 --second-window 5 38 --output work/pair-new.json --font C:/Windows/Fonts/arial.ttf
uv run --frozen python -m tools.weipin_experiment apply --manifest work/task.json --timeline work/fixed/timeline.json --roles work/roles.json --evidence work/pair-new.pair.json --prior-origins work/fixed/origin-bindings.json --output work/timeline-new.json --render-dir work/render-new
```

窗口参数是对应采样缓存的本地frame编号，不是秒数。`prior-origins`由已有受控来源收据生成：每项含`asset_id`、原件/代理的实际`path/size/mtime_ns`及`clock: same_source_clock`，不能按文件名猜来源。新目录存输出，该入口不写NAS、业务表或生产队列。

## 卫品42定向验证

固定原13句、直率英子1.2倍配音及现有音频。吸水合法组为第8–10句，共4.163719秒；第7句不闷热仍不可借用。

联合画面复核支持3431/3432、3513/3514、3513/3515配对，最后一组序号距离为2；3431/3514因木桌平铺与软垫承载场景冲突被拒绝。这是三个候选组合、涉及两个倒液场景，不应计为三次独立实验。041本文件没有按压，不代表041/042这一组缺少验证。

当前三个通过关系复核的候选组合在原速保护下均超出4.163719秒，分别记录时长冲突，没有输出新的合格整片。此前将跨文件本身判错的结论撤回；跨主题借时仍无效。真实媒体、模型请求、配对及拒绝收据留在包外隔离验证目录。
