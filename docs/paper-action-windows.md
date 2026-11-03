# 纸品动作事实与帧级窗口验证

这个验证入口把画面事实和句长求解分开。它只复用已有素材与配音，输出诊断选镜方案和可播放对照，不启动完整生产流水线。旧的 [纸品选镜回放](paper-selection-replay.md) 保留；新能力不替换默认生产流程。

## 先记录源帧事实

`orchestrator/media/action_frames.py` 使用ffprobe建立真实解码帧号与PTS账本，显式处理源起始时间。提取按解码帧号选择，并核对实际显示PTS，不能把请求时间直接写成已观察时间。分析图片与渲染图片按宽度分开缓存，保持原比例。

同模块的光流只生成局部运动线索和待加密区域，不填揉搓、接触或完整结果标签。相机运动、手遮挡等仍可能产生假信号，不能按运动峰值直接认证卖点。

`orchestrator/selection/paper_action_events.py` 复用已有GLM／千问适配、账户槽位与缓存。模型只看商品、原比例画面、真实帧号和观察区域；提示词不包含目标句长、所需速度和旧通过结论。输出必须引用本次实际给出的帧号，并保留边界不确定范围。位于观察首尾的事件标为censored，不能把“窗口开始时已经在揉”当首次开始动作。

这种分离减少目标影响观察的风险，不能保证模型判断正确。模型仍会把折叠、展开、下一次动作或静置误读，必须以源帧事实校准。改变源窗、输入图片或提示词后按已有身份重新复核，不触发全库识别。

## 再搜索窗口与合法速度

`orchestrator/selection/paper_action_windows.py` 提供纯函数：

- `enumerate_action_windows(frame_pts_us, phases, ...)` 枚举已复核范围内的连续源帧窗口。
- `plan_action_window(..., window_start_us, window_end_us, ...)` 求解单个窗口。
- `validate_action_plan(plan, ...)` 从输入重新核验方案及其实际选帧表，不信任存储的ok或proof标记。

调用者必须明确传入源FPS、固定目标起点/句长、结果最少输出帧数，以及已确认的连续相位。目标时间没有样本专属默认值。相位字段为整数微秒的`start_us/end_us`、`kind`和`can_speed`，可附边界不确定量。关键动作与结果不允许加速；可压缩过程优先≤2倍，必要时≤3倍。不能跳源窗、裁掉保护区、慢放或定格补足。

除了精确保持原句微秒时长，求解器按渲染器相同的绝对目标帧边界分配整数输出帧，检查每段实际帧域速度、保护时间、结果帧数和跨段源帧步进。当前实现只支持CFR源与整数源／输出FPS比；其他情况明确拒绝，不猜测变帧率映射。

原句时长不一定是整帧倍数，报告同时保留微秒时间、输出帧数与量化差，不能把两种时钟当成同一精度。`require_exact_frame_duration=True`可显式拒绝无法整帧相等的目标。结果最少帧数是调用者的试验策略，不是内置的视觉合格门槛。

## 显式源帧渲染与复查

解析方案的`proof.samples`是选帧计划，不是成片证据。普通`setpts → fps`可能使用帧中心采样，最终显示的尾帧会早于名义源窗末端；不能用源窗end判断结果已经进入成片。

`tools.selection_preview.render(..., source_frame_map=spec)`新增可选模式。合同含`index`、完整`plan`、`phases`、`source_identity`、`min_result_frames`和2／3倍上限。工具重探真实源PTS，重新验证方案、逐段覆盖及存储选帧表，再提取指定源帧；这句不经过第二次`fps`重采样或尾帧克隆。其他句、字幕、音轨保持原渲染行为。

CLI可使用：

```powershell
uv run --frozen python -X utf8 -m tools.selection_preview --draft $baseline --draft-root $originalDraftRoot --overrides $overrides --source-frame-map $frameMapContract --output $newPreviewDirectory --name review
```

完成后仍要完整解码，将实际输出帧与原源帧做独立匹配，确认最后结果帧、无重复/倒退、音轨与句边界保持。重复播放版本必须明确标示重复次数，不能通过放慢或延长单句伪装适配。

## 验证边界

纸品34仅揉搓句的隔离验证使用原qcfix A、原配音、字幕和BGM。候选覆盖、模型原始观察、人工源帧校准、窗口搜索、解码像素映射与观看结论分别留证。轻微指间摩擦是否足以呈现揉搓，应按实际画面判定，不能单靠关键词或强制要求揉成球。

回归见`test_paper_action_events.py`、`test_paper_action_windows.py`和`test_selection_preview.py`，覆盖真实60fps源、非零PTS、错误帧号、目标信息隔离、帧域超速、保护区裁断、伪造映射与实际解码帧对应。测试通过不能代替真实动作完整、结果可读、独立QC与人工验收。
