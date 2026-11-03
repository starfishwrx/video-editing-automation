# Windows 商品视频自动化

把商品素材和逐句口播，整理成带配音、字幕与背景音乐的可编辑剪映草稿。整个项目按 Windows 脚本运行：可以手动执行一轮，也可以交给任务计划程序定时触发。

当前 `main` 的工具包版本为 **1.6.0**。默认使用 **GLM-5.3-Flash** 分析画面、AI 选镜和无头剪映建稿；BGM 为空就自动选曲。正式交付前，必须完成独立视觉 QC 和当前输入、运行环境、草稿包的验收。

## 输入、流程与输出

准备同一商品的素材、逐句口播，以及单任务 `manifest.json` 或本地批次队列。模型凭据、剪映环境和交付位置由本机配置提供。

```mermaid
flowchart LR
    A[商品素材与口播] --> B[抽帧分析与选镜]
    B --> C[配音、字幕、BGM与时序]
    C --> D[无头剪映草稿]
    D --> E[独立视觉QC与自动验收]
    E --> F[按配置交付]
```

输出包括可编辑草稿、选镜结果、音画时间范围、音乐记录、QC 报告和任务状态。正式模式验收通过后，才进入配置的 NAS 分发和业务表回填；预览用于审片，保留未认证状态。

## 当前能力与范围

| 能力 | 当前状态 |
|---|---|
| 默认生产 | Windows 脚本串联素材分析、选镜、配音、字幕、无头建稿、独立 QC 与交付 |
| 视觉调用 | GLM 或 Qwen 直连；默认 `glm-5.3-flash`，通过火山方舟发送抽帧图片 |
| 自动音乐 | 空值自动选曲；明确指定的曲目优先；缺曲或有效源太短会阻断 |
| 可编辑交付 | 使用单独安装的无头后端生成原生草稿，按配置分发媒体与字体资源 |
| 独立时间线与渲染 | 显式开发入口，用于固定选镜后的工作预览；内容、观看效果和完整原生导出验收仍待完成 |
| 纸品局部变速 | 独立诊断工具，保留已有未通过结论，尚未接入默认生产 |

默认选镜为 `ai_editor`，最终视觉 QC 使用 v4 合同。`rule_then_ai` 和 `legacy` 是显式选择的选镜策略，与 GLM/Qwen 调用通道分别配置。模型完整名称、图片输入及覆盖顺序见[模型与视觉流程](docs/model-and-vision.md)。

运行不依赖编码 Agent、Mac 交接或人工 prepare/collect。默认无头建稿不控制剪映界面；历史写稿方式仅能显式选择，不会自动回退。

## 快速开始

需要 Windows、Git、Python **3.11 或 3.12** 和 [uv](https://docs.astral.sh/uv/)。先获取源码并准备 Python 环境：

```powershell
git clone https://github.com/starfishwrx/video-editing-automation.git
cd video-editing-automation/packages/video-v1.5.3
uv sync --frozen
Get-Content VERSION
uv run --frozen python -X utf8 doubao-jianying-orchestrator/run.py --help
```

后续命令均在这个包根执行。目录名 `video-v1.5.3` 是保留的历史路径，实际版本读取 `VERSION`。

先验证素材读取、抽帧和本机工具链，可按[快速开始](docs/quickstart.md)运行合成视频示例；该示例不调用视觉模型、TTS 或交付服务。

### 配置默认无头环境

FFmpeg 和 ffprobe 需要在当前进程的 `PATH` 中；便携工具准备方法见快速开始。无头后端是单独授权、安装的依赖，本仓库不分发该后端或剪映原生 DLL。所安装后端必须提供 `headless-production-library/v1`，并匹配其支持的本机剪映运行时；普通未扩展的上游检出不能直接替代。

先创建本机配置，保留已有文件：

```powershell
New-Item -ItemType Directory -Force work | Out-Null
if (-not (Test-Path work/config.local.ps1)) {
    Copy-Item examples/config.example.ps1 work/config.local.ps1
}
```

编辑 `work/config.local.ps1`，补齐下列配置。路径均为虚构示例；完整变量和依赖要求见[环境合同](packages/video-v1.5.3/ENVIRONMENT.md)。

| 配置 | 用途 |
|---|---|
| `JIANYING_HEADLESS_ROOT` | 已授权且通过校验的无头后端目录，例如 `C:/example/headless-backend` |
| `JIANYING_INSTALL_DIR` | 与后端匹配的剪映原生运行时目录，例如 `C:/example/jianying-runtime` |
| `JY_HEADLESS_FONT` | 静态 TTF/OTF 字体，例如 `C:/example/fonts/subtitle.ttf` |
| 草稿库 | 将 `JY_DRAFT_ROOT`、`JY_DRAFTS_ROOT`、`JY_PROJECTS_ROOT` 指向同一个独立目录，例如 `C:/example/drafts`；置于剪映首页草稿库之外 |
| 模型凭据 | 默认读取 `ARK_API_KEY`，为空则读取 `JY_VOLC_API_KEY`；密钥由本机安全配置提供 |

加载配置并建立独立草稿目录后，单独运行无头预检：

```powershell
. ./work/config.local.ps1
New-Item -ItemType Directory -Force $env:JY_DRAFT_ROOT | Out-Null
uv run --frozen python -X utf8 doubao-jianying-orchestrator/run.py env
if ($LASTEXITCODE -ne 0) { throw '请先处理无头环境检查中的阻断项。' }
```

`env` 默认检查生产环境，包含所选写稿后端，未就绪会返回非零退出码。`env --input work/task/manifest.json` 按任务指定的后端和字体检查；`env --profile base` 只检查通用工具链。环境检查不认证云端密钥，建稿与恢复预检也会在付费阶段前检查所选后端。

配置读取进程环境变量，复制 `.env` 不会自动加载。切换千问时，明确设置 `JY_VISION_TRANSPORT=qwen` 及 `JY_QWEN_BASE_URL`、`JY_QWEN_MODEL`、`JY_QWEN_API_KEY`；详见模型与视觉流程。

### 运行一轮任务

批处理入口读取本地队列，任务获取方负责写入队列；这个入口不会自行从飞书抓取任务。先复制示例，在私有副本里填写真实素材、文案、音色和任务标识：

```powershell
if (-not (Test-Path work/batch_queue.json)) {
    Copy-Item examples/queue.example.json work/batch_queue.json
}
# 编辑 work/batch_queue.json 后，仅查看本轮执行计划。
powershell -NoProfile -File scripts/run_pipeline.ps1 -DryRun
```

检查计划和输入后，执行完整一轮；这一步会调用配置的模型与配音服务：

```powershell
powershell -NoProfile -File scripts/run_pipeline.ps1
```

队列的 `delivery_mode` 省略时按 `preview` 处理。需要正式交付时明确填写 `formal`，并配置相应的 NAS／表格连接；四项验收通过后才会分发和回写。只在本机留稿可设置 `JY_SKIP_SHIP=1`。

手动与定时运行使用同一入口。注册任务计划程序是单独操作，路径参数、队列字段、重试预算及状态文件见[Windows 脚本流水线](docs/windows-pipeline.md)。

单任务也可使用 manifest CLI；准备好输入后，显式选择预览模式并使用新的报告目录：

```powershell
uv run --frozen python -X utf8 doubao-jianying-orchestrator/run.py build `
  --input work/task/manifest.json --report-dir work/task/preview-01 `
  --delivery-mode preview
```

## 验收与恢复

正式交付检查当前输入身份、运行时身份、草稿包和独立视觉 QC，四项均须 `PASS`。生成草稿、离线测试或合成预览只能证明对应技术步骤，真实画面、声音和剪映可编辑性仍需按[测试策略](docs/test.md)验收。预览、超时或证据缺失保留未认证／阻断状态。

通过 `work/pipeline_runs/`、`work/pipeline_state.json` 和任务的 `report/`、`eval/` 查看计划、失败原因和恢复次数。定向恢复优先复用现有素材观察、配音和草稿，具体操作见 Windows 脚本流水线；不要删除整个状态文件来重放全部任务。

分阶段恢复读取已成功匹配且绑定当前原始输入的选镜报告，再进入写稿；缺失、失败、损坏或不属于本次输入的报告会阻断。输入变化或旧报告没有输入绑定时，需基于已有素材分析显式执行 `semantic_match`，不会自动重跑全库识别或配音。

## 项目结构

```text
.
├── README.md / AGENTS.md / CONTRIBUTING.md
├── docs/                         # 使用、架构、测试、开发记录
├── packages/video-v1.5.3/         # 当前维护源码，版本读 VERSION
│   ├── doubao-jianying-orchestrator/
│   │   └── orchestrator/          # 编排、选镜、视觉、音频、媒体等功能包
│   ├── jianying-editor/           # 编辑器封装与兼容能力
│   ├── tools/                    # 队列、验收、交付、开发工具
│   ├── scripts/                  # Windows 运行与定时注册入口
│   ├── examples/                 # 虚构示例与配置模板
│   └── tests/                    # 包级回归，编排器另有 tests/
├── .github/                      # 离线 CI 与 PR 模板
└── releases/                     # 历史清单与候选资产
```

`packages/video-v1.4.0/` 至 `video-v1.5.2/` 为只读历史包；1.5.4/1.5.5 为独立索引实验路线。新功能在当前维护包的对应模块中开发，旧平铺导入只保留兼容职责，归属见[模块化开发](docs/modular-development.md)。

## 阅读与开发入口

| 要做的事 | 从这里开始 |
|---|---|
| 首次使用与配置 | [快速开始](docs/quickstart.md)、[环境合同](packages/video-v1.5.3/ENVIRONMENT.md) |
| 完整流水线与定时运行 | [Windows 脚本流水线](docs/windows-pipeline.md) |
| 模型、选镜与音乐 | [模型与视觉流程](docs/model-and-vision.md)、[音频说明](docs/subsystems/audio-voice.md) |
| 开发与模块维护 | [AGENTS.md](AGENTS.md)、[模块边界](docs/modular-development.md)、[贡献约定](CONTRIBUTING.md) |
| 独立时间线工作预览 | [程序化生产](docs/programmatic-production.md)、[实施方案](docs/plans/20261101-programmatic-production.md) |
| 纸品动作诊断 | [选镜回放](docs/paper-selection-replay.md)、[帧级动作窗口](docs/paper-action-windows.md) |
| 验证、打包与历史结果 | [测试策略](docs/test.md)、[发布流程](docs/release-process.md)、[版本记录](docs/RELEASES.md) |

日常使用 `main + 短期功能分支 + PR`，分支前缀为 `feat/`、`fix/`、`docs/`。小改动只跑对应模块及直接调用方；跨模块或正式门禁变化才本地全量，PR CI 保持两套离线回归。发版在最终解压目录运行一次全量，实际安装另行验证。

在包根可运行模块边界检查，其他命令按测试策略选择：

```powershell
uv run --frozen python -X utf8 -m tools.check_architecture
```

## 版本与下载

维护源码固定在 `packages/video-v1.5.3/`，实际版本读取包内 `VERSION`。下载包的版本和附件以 [GitHub Releases](https://github.com/starfishwrx/video-editing-automation/releases) 为准，历史收据见版本记录；生产安装身份由部署机器的 `ACTIVE_VIDEO_RUNTIME.json` 和实际安装文件确认。

需要从当前源码构建包时，使用发布流程中的 `tools.release_package`。源码合并、发布附件和生产切换分别留证；功能通过离线检查后，仍按既定真实任务观察和人工验收要求发布或部署。

## 使用边界与隐私

This repository is designed to be publishable with only fake examples.

模型会接收实际抽帧和相关脚本文字，运行前确认素材适合发送到所配置的服务。密钥、业务素材、模型响应、审片记录和输出留在本机忽略的 `work/` 或其他私有目录；公开示例使用虚构值。历史包用于追溯，不把其中旧机器配置或业务标识复制为新任务配置。第三方后端和剪映运行时按各自授权独立准备。
