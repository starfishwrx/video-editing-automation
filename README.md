# 剪映自动化工具包

面向视频生产团队的豆包与剪映自动化工具包。仓库按版本保存任务编排脚本、剪映工程辅助工具、视觉规则、环境说明和测试，便于团队阅读、评估、交接与下载。

> 当前源码候选：**工具包 VERSION = 1.5.6**，源码目录仍为 `packages/video-v1.5.3/`（历史代号，不随小版本改名）。编排器与剪映组件随 `VERSION`；视觉规则独立为 **1.4.1**。候选包和测试结果不代表渲染机已经升级或生产验收完成。

文档分层（对齐 [grapeot 项目脚手架](https://github.com/grapeot/context-infrastructure/blob/main/rules/skills/project_scaffold.md)）：先读 [`AGENTS.md`](AGENTS.md) 与 [`docs/README.md`](docs/README.md)，再按需打开 `docs/prd.md` / `rfc.md` / `working.md` / `test.md`。

1.5.6 在 1.5.3 交付恢复与合包技能之上，纳入选镜审稿修订与全局配音无气口等规则；合包命令见[已验证修复合包技能](packages/video-v1.5.3/doubao-jianying-orchestrator/references/validated_fix_to_release.md)。

## 下载与校验

1. 打开 [GitHub Releases](https://github.com/starfishwrx/video-editing-automation/releases)。
2. 选择需要的版本，下载对应 ZIP 和 `SHA256SUMS` 校验文件。
3. 将 ZIP 和校验文件放在同一目录，先验证文件完整性，再解压阅读或部署。
4. 部署前阅读该版本的 `ENVIRONMENT.md`、`RULES.md` 和 `README.md`，按目标机器准备环境与配置。

也可以直接浏览 [`packages/`](packages/) 中的对应版本源码。当前主线以目录内 `VERSION` 为准。

## 使用流程

1. 根据版本表和 [`docs/RELEASES.md`](docs/RELEASES.md) 确认版本、来源和发布状态。
2. 阅读版本目录中的环境约定、规则及编排器说明，确认操作系统、运行依赖和所需路径。
3. 在渲染环境配置访问凭据与机器路径；这类配置不随源码包提供，也不要提交到仓库。
4. 先运行该版本的自动化测试（见 [`docs/test.md`](docs/test.md)），再按部署流程进行真实任务观察和人工验收。

不要覆盖正在运行的队列；真实任务观察和人工验收通过前，不应把候选版当作已上线版本。

## 使用要求

- 目标机器需满足对应版本 `ENVIRONMENT.md` 中列出的运行依赖（统一用 **uv**）。
- 豆包、剪映及相关服务和素材路径需能从目标机器访问。
- 将账号令牌、密码和机器专属路径保存在部署环境，不要写进源码或提交到 GitHub。
- 使用前确认目标版本状态；下载、安装或测试通过都不能代替渲染机部署与生产验收。

## 项目结构

- `AGENTS.md`：给 Agent 的路由表。
- `LESSONS.md`：错题本。
- `docs/`：PRD/RFC/working/test、体系结构、子系统与发版说明（见 [`docs/README.md`](docs/README.md)）。
- `packages/video-v1.4.0/` … `video-v1.5.2/`：历史归档，禁止修改。
- `packages/video-v1.5.3/`：当前活代码（`VERSION` = 1.5.6）。
- `releases/`：各版本 ZIP 对应的 SHA-256 清单与候选 ZIP。

## 版本记录

| 版本 | 组件版本 | 说明 |
|---|---|---|
| **v1.5.6**（活代码在 `video-v1.5.3/`） | 工具包 / 编排器 / 剪映组件 1.5.6；视觉规则 1.4.1 | 选镜审稿修订；全局配音无气口；在 1.5.3 交付恢复与合包技能之上继续迭代。源码候选，部署另计。 |
| **v1.5.3** | 工具包、编排器、剪映组件 1.5.3；视觉规则 1.4.1 | 共享交付检查实际素材路径；授权恢复原始生成版；修复合包技能与可重建 ZIP 验收工具。 |
| **v1.5.1** | 工具包、编排器、剪映组件 1.5.1；视觉规则 1.4.1 | 预览黑屏补齐策略与 GPU/4K 解码说明。 |
| **v1.5.0** | 工具包、编排器、剪映组件 1.5.0；视觉规则 1.4.1 | 评估与队列保护、素材索引和跨机器审阅。 |
| **v1.4.0** | 工具包、编排器、剪映组件及视觉规则均为 1.4.0 | 历史归档版。 |

## 发布验收

发布前应从下载 ZIP 全新解压，核对 SHA-256 与包内文件清单，并运行包测试和编排器测试。完成这些检查只说明源码包通过了对应验证；渲染机部署还需单独备份、升级、运行真实任务并完成验收。详见 [`docs/release-process.md`](docs/release-process.md) 与 [`docs/RELEASES.md`](docs/RELEASES.md)。
