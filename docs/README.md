# 文档索引

文档分层对齐 [grapeot/context-infrastructure](https://github.com/grapeot/context-infrastructure) 的项目脚手架：`prd` / `rfc` / `working` / `test`，再加本仓库已有的子系统与发版说明。

## 初次使用

- [Windows 整体脚本与定时运行](windows-pipeline.md)：完整流水线、配置、状态和恢复。
- [快速开始](quickstart.md)：安装、离线验证、合成抽帧示例与真实任务预览入口。
- [模型与视觉流程](model-and-vision.md)：默认 GLM-5.3-Flash 的完整型号、图片输入与配置优先级。
- [环境配置](../packages/video-v1.5.3/ENVIRONMENT.md)：本地依赖、模型/API、路径与生产集成变量。
- [贡献与分支约定](../CONTRIBUTING.md)：功能分支、PR、CI、tag/Release 的职责。
- [字幕安全区与居中](subtitle-layout.md)：自动换行、位置保护、无头后端依赖和验证边界。

当前维护包是 `video-v1.5.3/`（本功能分支候选 VERSION 为 1.6.0）；1.5.4/1.5.5 是隔离索引实验。
下载包与生产安装分别核对，见根 README 的[版本与下载](../README.md#版本与下载)。

## 当前边界与接续位置

1.6.0 默认建稿已接无头后端及空 BGM 自动选曲，并贯通当前原生稿 QC、NAS 与版本恢复；正式质量门禁继续生效。实际本机启用状态读取 ACTIVE_VIDEO_RUNTIME.json；独立时间线与完整原生导出验收的边界见程序化生产页面。

- 默认生产：Windows 脚本 → 素材/选镜/配音 → 剪映写稿 → 独立视觉 QC → 四项自动验收。源码候选、GitHub 集成与生产安装分别确认。
- 已有基础：功能包迁移、旧 Agent 调度退役、GLM/Qwen 直连及 Windows 验收。旧平铺模块仅为兼容导入，实际归属见[模块化开发](modular-development.md)。
- 隔离验证：[纸品选镜回放](paper-selection-replay.md)和[动作事实/帧级窗口](paper-action-windows.md)只用于诊断，不代表默认生产或真实内容已通过。
- 显式开发入口：[独立时间线与无草稿工作预览](programmatic-production.md)已实现固定选镜、音频/字幕准备、独立渲染与检查骨架；技术结果保持未认证。动作自动接入、内容/观看验收、末端导出与调度恢复仍按[实施方案](plans/20261101-programmatic-production.md)推进。
- 验证入口：[测试策略](test.md)区分离线程序检查、真实视频效果和剪映可编辑性；本轮具体结果见[开发记录](working.md)。

## 维护与 Agent 入口

| 文件 | 给谁 | 内容 |
|---|---|---|
| [`../AGENTS.md`](../AGENTS.md) | AI Agent | 路由表、禁令、本地验证命令 |
| [`prd.md`](prd.md) | 人 / Agent | 目标、用户、成功标准 |
| [`rfc.md`](rfc.md) | 人 / Agent | 架构边界与定案 |
| [`working.md`](working.md) | Agent | 按日 changelog |
| [`test.md`](test.md) | Agent | 什么算验证完成 |
| [`../LESSONS.md`](../LESSONS.md) | Agent | 错题本（可晋升硬规则） |

## 体系与规范

| 文件 | 内容 |
|---|---|
| [`architecture.md`](architecture.md) | 端到端数据流 |
| [`modular-development.md`](modular-development.md) | 架构审计、功能包边界、旧入口与新增功能开发规则 |
| [`code-standards.md`](code-standards.md) | 微秒口径、闸门形态、凭据、依赖 |
| [`agent-loops.md`](agent-loops.md) | Issue/反馈/夜间审计三类循环提示词 |
| [`release-process.md`](release-process.md) | 合包发版流程 |
| [`RELEASES.md`](RELEASES.md) | 各版本归档与 ZIP 收据 |

## 会议与功能计划

- [独立时间线、生产验收与末端剪映导出](plans/20261101-programmatic-production.md)：基于 `feat/paper-shot-retiming` 的分期方案，保留可编辑草稿交付；当前实现和剩余边界见[开发入口](programmatic-production.md)，生产未切换。

| 文件 | 内容与状态 |
|---|---|
| [2026-10-26 会议功能计划](plans/20261026_feature_plan.md) | 原计划与验收标准；功能源码候选已提交，部署和完整成片验收待完成 |
| [会议功能实现记录](meeting-features-20261026.md) | F01–F08行为、配置、无头验证及未完成验收项 |

## 隔离诊断

- [动作的卖点信息价值](shot-selling-point-value.md)：至少一个具体卖点，不强求整句覆盖；优先判断动作/展示在传达什么，再参考可读性。033与036通用动作反例、076照光相关表达正例。

- [展示镜头可用性与单句优化](shot-usability.md)：无文案可读性观察、目标特征区间交集、遮挡/整理拒绝和最终源窗比选；卫品42第7句固定输入验证，不把同商品或高置信度当作有效镜头。

- [卫品实验配对与局部变速](weipin-experiments.md)：同次实验允许跨文件；`phase-template`保存已复核区间，`optimize`生成固定配音下的独立时间线、逐句变化表及工作预览，自动边界和逐句相位仍待复核。

- [视频时序观察与跨句视觉组](temporal-shot-groups.md)：Qwen实际视频输入、越界时间戳拒绝、固定音轨下的相邻句视觉编排与未验收边界。

- [按口播目的识别与选镜](plans/20261102-purpose-driven-selection.md)：任务限定索引、连续源窗复核、固定配音求解和实际输出盲审；卫品42隔离验证，默认生产未切换。

- [纸品选镜单模块验证](paper-selection-replay.md)：固定原音轨与句长的 A/B 诊断入口，保留未解决项与失败结论。
- [纸品动作事实与帧级窗口](paper-action-windows.md)：源帧事件观察、有限 CFR/FPS 支持及实际选帧复查。

## 子系统

路径一律相对活包 `packages/video-v1.5.3/`（真实版本见该目录 `VERSION`）。

| 文件 | 主题 |
|---|---|
| [`subsystems/orchestrator-engine.md`](subsystems/orchestrator-engine.md) | 引擎闸门与状态 |
| [`subsystems/orchestrator-cli.md`](subsystems/orchestrator-cli.md) | CLI 子命令 |
| [`subsystems/visual-matching.md`](subsystems/visual-matching.md) | 选镜与视觉证据 |
| [`subsystems/audio-voice.md`](subsystems/audio-voice.md) | TTS / BGM / 响度 |
| [`subsystems/timing.md`](subsystems/timing.md) | 时序契约 |
| [`subsystems/media-codec.md`](subsystems/media-codec.md) | 编解码 / ffmpeg / GPU |
| [`subsystems/runtime-and-recovery.md`](subsystems/runtime-and-recovery.md) | 容量、诊断、恢复 |
| [`subsystems/jianying-editor.md`](subsystems/jianying-editor.md) | 草稿写入边界 |
| [`subsystems/package-tools.md`](subsystems/package-tools.md) | 队列、闭环、评估工具 |

## 包内说明（随 VERSION 走）

| 路径 | 内容 |
|---|---|
| `packages/video-v1.5.3/README.md` | 候选包说明与 uv 命令 |
| `packages/video-v1.5.3/ENVIRONMENT.md` | 环境变量合同 |
| `packages/video-v1.5.3/RULES.md` | 视觉业务规则 1.4.1 |
| `.../doubao-jianying-orchestrator/SKILL.md` | 编排技能入口 |
| `.../doubao-jianying-orchestrator/references/` | 运行时合同、交付恢复、质量课、合包技能 |
| `packages/video-v1.5.3/docs/` | 选镜审稿、阶段性更新、卫品历史踩坑 |
