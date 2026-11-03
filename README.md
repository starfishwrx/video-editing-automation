# 剪映自动化工具包

这是一套纯 Windows 脚本流水线：定时读取任务队列，调用 GLM 或千问分析素材、选镜、生成配音与剪映草稿，执行独立视觉验收，再按配置交付。功能按模块维护，运行无需编码 Agent 或 Mac 交接。

**当前默认视觉模型是 GLM-5.3-Flash，API ID 为 `glm-5.3-flash`。** 通过火山方舟直接发送抽帧图片；GLM-5.3、GLM-5.3-Flash 和 ChatGLM3 是不同型号，不能混写。模型输入、配置覆盖与源码位置见[模型与视觉流程](docs/model-and-vision.md)。

## 输入、流程与输出

- 输入：同一商品的素材视频、逐句口播及任务 `manifest.json`；运行账号和机器路径由本机配置提供。
- 流程：FFmpeg 分镜/抽帧 → GLM-5.3-Flash 理解画面 → AI 复审候选并编排 → 程序校验源窗口与音频时长 → 剪映写稿 → 最终切片独立视觉 QC → Windows 自动验收 → 按配置交付。
- 输出：剪映草稿、选镜计划、时间范围及 QC/任务结果 JSON。生成草稿与正式交付分别验收。

默认选镜模式为 `ai_editor`，最终视觉 QC 使用 v4 合同；`rule_then_ai` 为显式规则初排/AI 复核策略，`legacy` 保留旧算法。算法选择与 GLM/Qwen 调用通道分别配置，各自报告不能混用。详见[架构](docs/architecture.md)和[选镜说明](packages/video-v1.5.3/docs/AI_EDITOR.md)。

当前分支的功能边界与接续任务见[文档入口](docs/README.md#当前边界与接续位置)。纸品动作/帧级窗口是隔离验证；[程序化生产与末端剪映导出](docs/plans/20261101-programmatic-production.md)仍为待实施方案，默认生产尚未改为独立时间线。

## 快速开始

Windows + Python **3.11 或 3.12**，依赖统一用 [uv](https://docs.astral.sh/uv/)。从仓库根执行：

```powershell
cd packages/video-v1.5.3
uv sync --frozen
uv run --frozen python -X utf8 doubao-jianying-orchestrator/run.py --help
```

完整流水线与 Windows 定时入口见[Windows 脚本运行](docs/windows-pipeline.md)。接着按[快速开始](docs/quickstart.md)操作：先运行离线测试和合成视频抽帧，再配置真实素材、模型与剪映环境进行预览建稿。示例位于包内 [`examples/`](packages/video-v1.5.3/examples/)，不需要把账号或业务素材提交到 GitHub。

完整配置见 [`ENVIRONMENT.md`](packages/video-v1.5.3/ENVIRONMENT.md)。运行时只调用 GLM 或 Qwen 的直连 HTTP 接口。默认 `JY_VISION_TRANSPORT=volc_glm`（别名 `glm` / `volc`），模型为 `JY_VISION_MODEL=glm-5.3-flash`，凭据读取 `ARK_API_KEY`，为空时读取 `JY_VOLC_API_KEY`；千问使用 `qwen`（别名 `qwen_compatible`）及 `JY_QWEN_*` 配置。已移除的旧通道和未知值均报错，不自动回落。通用配置从进程环境变量读取；复制 `.env` 文件不会自动加载。

## 版本与下载

版本状态按 **2026-11-02** 核对；后续以包内 `VERSION` 和 [GitHub Releases](https://github.com/starfishwrx/video-editing-automation/releases) 的实际附件为准。本文描述当前分支，功能分支的能力不等于已合入 `main`。

- **当前维护主线**：[`packages/video-v1.5.3/`](packages/video-v1.5.3/)，`VERSION` 为 **1.5.6**。目录名是历史代号，升级时保留路径。
- **已发布下载包**：本次核对只有 [video-v1.5.1 预发布版](https://github.com/starfishwrx/video-editing-automation/releases/tag/video-v1.5.1)。它与当前主线存在差异；仓库内的旧 ZIP 也不代表 1.5.6 已发布。
- **实验路线**：`packages/video-v1.5.4/`、`packages/video-v1.5.5/` 是独立的索引/检索候选，后者声明为 experimental；不作为默认运行入口。
- **生产安装**：由部署机器的 `ACTIVE_VIDEO_RUNTIME.json` 与实际安装文件确认。源码、下载包与生产版本分别记录。

需要当前源码候选时，从主线检出后按[合包流程](docs/release-process.md)生成新包。候选测试通过后，仍需完成既定的真实任务观察和人工验收，才能发布或部署。

下载已有发布包时，将对应 ZIP 与 `SHA256SUMS` 放在一起，核对文件名和哈希，再解压阅读该版本的环境与规则说明。完整历史收据见 [`docs/RELEASES.md`](docs/RELEASES.md)。

## 阅读与开发入口

- 初次使用：[快速开始](docs/quickstart.md)、[环境配置](packages/video-v1.5.3/ENVIRONMENT.md)、[模型与视觉流程](docs/model-and-vision.md)。
- 了解实现：[文档索引](docs/README.md)、[架构](docs/architecture.md)、[产品目标](docs/prd.md)。
- 修改项目：[贡献与分支约定](CONTRIBUTING.md)、[`AGENTS.md`](AGENTS.md)、[测试策略](docs/test.md)。
- 按功能开发：[模块化开发与架构审计](docs/modular-development.md)，查找功能包并运行依赖边界检查。
- 打包交接：[发布流程](docs/release-process.md)、[版本记录](docs/RELEASES.md)。

日常使用 `main + 短期功能分支 + PR`；发布版本用 tag/Release 标记。仓库内的离线 CI 验证代码合同，真实模型效果、剪映打开与人工成片验收分别留证。

## 项目结构

- `docs/`：面向使用者和维护者的入口、架构、测试及发版说明。
- `AGENTS.md` / `LESSONS.md`：Agent 路由与维护教训。
- `packages/video-v1.5.3/`：当前维护代码、示例、依赖锁文件和两套测试。
- `packages/video-v1.4.0/` … `video-v1.5.2/`：只读历史包。
- `packages/video-v1.5.4/`、`video-v1.5.5/`：隔离实验路线，说明以各自 README 为准。
- `releases/`：历史校验清单与候选 ZIP；正式可下载资产以 GitHub Release 页面为准。
- `.github/`：离线 CI 与 PR 模板。

## 使用边界与隐私

This repository is designed to be publishable with only fake examples.

模型会接收任务抽帧和相关脚本文字；运行前确认这些素材适合发送到所配置的服务。源码示例不包含真实账号、生产日志或业务素材，私有配置和输出保留在本机。预览稿和模型调用失败保留未认证状态；自动正式交付必须具备当前草稿的真实视觉证据；模型失败、超时和预览均不能标成正式通过。
