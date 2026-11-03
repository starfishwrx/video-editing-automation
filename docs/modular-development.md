# 模块化开发

本轮以 main `7775331` 为审计基线。维护源码仍在 `packages/video-v1.5.3/`，历史包和实验包不迁移；安装组件和 `run.py`、worker、闭环命令的外部路径保留。

## 审计结论与实施边界

原编排器把 54 个实现模块放在同一目录，目录不能表达功能归属。引擎、素材分析、视觉、CLI 各承担多个阶段，局部功能需要跨多个大文件查找。部分模块存在运行时双向依赖，不能只靠移动目录声称已消除。两份闭环实现已经漂移，根入口缺少当时的 Eval/分发身份检查，tools 入口又在包外执行时因过晚初始化路径而失败。

本轮把现有实现迁入功能子包、将旧路径收缩为兼容入口、统一闭环实现，并增加自动检查阻止业务代码重新写回旧入口。保留现有函数和报告合同，使用全部离线回归检查迁移。后续细化大模块时以功能包为单位提交，不复制版本目录或改第三方 vendor。

## 功能边界

| 功能包 | 职责 |
|---|---|
| `domain` | 纯数据模型 `Clip`，不加载引擎或外部服务 |
| `application` | CLI 和端到端引擎，组合功能模块 |
| `audio` | 音色、TTS、BGM、响度和字幕 |
| `media` | FFmpeg、编解码、GPU、稳定窗口、帧质量 |
| `runtime` | 环境、容量、资源、并发、文件安全和任务日志 |
| `policy` | 品类规则、语义与动作规则、反馈和制作参数 |
| `selection` | 选镜、源窗口、分句、时序和使用台账 |
| `vision` | 远端模型调用、素材观察与缓存 |
| `quality` | 独立 QC、证据合同、草稿安全与诊断 |
| `delivery` | 草稿分发与恢复 |
| `tools/workflow` | Windows 整体定时脚本、队列入口和闭环实现 |
| `tools/acceptance` | 输入/运行时/草稿包/独立 QC 验收与交付协调 |
| `tools/queueing` | worker 的输入、终态、失败分类、任务选择和并行互斥合同 |

`orchestrator/module_map.json` 列出 50 个实现模块的归属和旧路径映射。`ai_editor_contract.py`、`visual_qc_contract.py`、`rulepack.py` 保持文件位置，服务独立 Eval 的按文件加载：它们必须读取同一包副本的规则，不能换成机器上已安装的旧版本。`ship_distribute.py` 同样保留原位置，服务独立交付加载与包级资源定位。它们是有意保留的公共合同/适配器，未复制到第二处。

除目录迁移外，本轮实际抽出三组职责：`application/engine.py` 使用 `domain/clips.py` 的数据模型；`selection/shot_analyzer.py` 的 FFmpeg 操作委托给 `media/frames.py`；worker 的纯合同进入 `tools/queueing/contracts.py`。媒体入口以参数传入 decoder、单帧回退和开关，保留旧入口的 patch 合同，媒体层不反向依赖选镜层。

## 迁移合同

- 实现只保留一份，旧导入和新导入共享模块状态；已有 mock/patch、缓存、锁和统计保持一致。
- 旧脚本的直接执行、`python -m` 与外部按文件加载的合同仍可使用。
- 规则、版本和技能路径从固定资源根解析，不随实现文件深度改变。
- `orchestrator` 包入口延迟加载引擎，读取版本与纯合同不再自动加载整个引擎。
- 正式 PASS、预览未认证、音频时序和视觉证据规则不变；不执行生产部署。

## 开发与验证

改动先定位功能包；新增功能放入对应包，跨包引用使用实现的真实路径，兼容入口只服务旧调用者。大模块的进一步拆分要围绕可测试输入输出，避免用 mixin 或动态函数拼装隐藏共享状态。

在活包根运行 `uv run --frozen python -X utf8 -m tools.check_architecture`。Windows CI 和默认包级测试都会执行相关边界检查：旧入口不得承载业务实现；实现模块不得反向引用旧导入路径；下层不得依赖 application；runtime/media 不得依赖选镜、视觉、音频、规则、质检或分发；domain 不得依赖其他功能包。函数内部的延迟 import 也会检查。新增功能模块可以直接放进功能包，无需增加旧入口；只有为既有调用路径迁移时才维护 module_map。

根级公共合同也检查导入，不能反向加载application或worker/交付工具；原位分发适配器保留其tools依赖。`tools/queueing` 不加载worker、其他tools层或编排业务；`tools/acceptance` 不反向依赖worker、workflow或dispatcher；tools三层都拒绝应用层及旧兼容导入。规则覆盖静态Python导入语句（含函数内部）；按文件动态加载继续由独立合同测试验证。

本轮保留了原来选镜、场景规则与视觉观测之间的延迟双向调用，以及 runtime/media 的环境适配关联。引擎和 CLI 仍有较大的应用编排函数；本轮没有把这些共享状态隐蔽地拆成 mixin，也不声称已经得到完全无环依赖图。后续可用显式的分析结果/任务上下文进一步分离准备、规划和写稿服务，逐条增加依赖约束。

验证命令和本轮结果见 `test.md`、`working.md`。本次是源码重构，不升级 VERSION，不重写已有发布清单，不安装生产。新布局的合包验证在包外副本执行，避免把开发验证当作正式发布。

## 视觉服务与旧入口清理

2026-10-30 的后续清理移除了 Codex CLI 与 Anthropic/CCSwitch 后端，只保留 GLM 和 Qwen 直连。服务选择由 `runtime/vision_config.py` 统一解析，未知或已删除的通道明确报错；建稿和生成恢复在配音/模型调用前保存环境阻断收据，只读草稿诊断仍可使用。

描述选镜改名为 `vision_analyzer.match_sentences`；`ai_editor`、`rule_then_ai` 与 `legacy` 仍是可用的选镜算法。移除旧命令组装、进程终止、profile/provider/schema 配置、Anthropic env 文件加载和非 HTTP 重试/槽位分支。HTTP 尝试与账户限流继续由共享客户端统一持有。

历史 JyPoll/JyRun 依赖整条 Agent 执行链，两个源码入口已改为立即返回退出码 2 的退役提示，不再调度或改接 worker。整体任务使用新的 Windows 脚本入口，内部复用 dispatcher；不修改宿主计划任务、既有生产脚本或部署。

观察和相位缓存保留固定 `profile="volc"` 的历史磁盘命名空间，不再接受运行时配置。GLM/Qwen 四组旧新缓存键对照一致，身份匹配的旧观察继续复用。旧进程/派发实现测试移除，通用失败阻断与提示词合同迁入 HTTP 测试，并新增退役入口无副作用、无效配置提前阻断和只读诊断回归。

## Windows 脚本与验收模块

整个项目的运行入口为 `scripts/run_pipeline.ps1`，内部调用 `tools/workflow/scheduled.py` 和 dispatcher；Windows 计划任务只负责周期触发这一完整入口。验收计算集中在 `tools/acceptance`，worker 负责调度适配与阶段调用。交付上下文显式传参，不修改 worker 全局路径或调用方环境。

Mac 交接及人工准备／收集已删除，正式交付只依据四项当前证据。历史导入仅保留仍有效的 `production_eval` 兼容层，不为退役交接保留可执行实现。详细运行方式见 [Windows 脚本流水线](windows-pipeline.md)。本次真实删除旧路径，因此同步路径型库存清单；不新增文件哈希门槛，不升级 VERSION。
