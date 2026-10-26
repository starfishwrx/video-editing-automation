# 文档索引

文档分层对齐 [grapeot/context-infrastructure](https://github.com/grapeot/context-infrastructure) 的项目脚手架：`prd` / `rfc` / `working` / `test`，再加本仓库已有的子系统与发版说明。

## 初次使用

- [快速开始](quickstart.md)：安装、离线验证、合成抽帧示例与真实任务预览入口。
- [模型与视觉流程](model-and-vision.md)：默认 GLM-5.3-Flash 的完整型号、图片输入与配置优先级。
- [环境配置](../packages/video-v1.5.3/ENVIRONMENT.md)：本地依赖、模型/API、路径与生产集成变量。
- [贡献与分支约定](../CONTRIBUTING.md)：功能分支、PR、CI、tag/Release 的职责。

当前维护包是 `video-v1.5.3/`（VERSION 为 1.5.6）；1.5.4/1.5.5 是隔离索引实验。
下载包与生产安装分别核对，见根 README 的[版本与下载](../README.md#版本与下载)。

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
| [`code-standards.md`](code-standards.md) | 微秒口径、闸门形态、凭据、依赖 |
| [`agent-loops.md`](agent-loops.md) | Issue/反馈/夜间审计三类循环提示词 |
| [`release-process.md`](release-process.md) | 合包发版流程 |
| [`RELEASES.md`](RELEASES.md) | 各版本归档与 ZIP 收据 |

## 会议与功能计划

| 文件 | 内容与状态 |
|---|---|
| [2026-10-26 会议功能计划](plans/20261026_feature_plan.md) | 音色与TTS倍速、音频字幕、BGM、选镜及反馈计划；仅规划，未实现或部署新增需求 |

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
