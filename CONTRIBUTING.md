# 贡献、分支与发布约定

维护前先读 [`AGENTS.md`](AGENTS.md)、[架构](docs/architecture.md)和[测试策略](docs/test.md)。当前活包是 `packages/video-v1.5.3/`，实际版本读 `VERSION`；历史包和索引实验保持各自边界，不复制整套代码来表示日常小版本。

## 日常分支

采用 `main + 短期功能分支 + PR`，目前不要求长期 `dev`。

- `main`：通过既定代码检查的集成基线；是否已经发布或部署需看独立收据。
- `codex/<简短主题>` 等功能分支：一次解决一个相关问题，改完通过 PR 合入 `main`；使用不同 Agent 时保留独立分支/工作树。
- tag：某次确定的代码快照。Release：该 tag 对应的说明、ZIP 和校验附件。部署机器实际运行身份单独回读。

从干净工作区开始，例如补快速开始文档：

```powershell
git switch main
git pull --ff-only
git switch -c codex/docs-quickstart
# 编辑、核对命令并验证；只暂存本次文件。
git add README.md docs/quickstart.md
git diff --cached
git commit -m "docs: add runnable quickstart"
git push -u origin codex/docs-quickstart
```

创建以 `main` 为目标的 PR，检查 Files changed、Checks 和审阅意见后再合并。合并后删除已完成的功能分支。不直接 push `main`，不将无关修改、临时数据或其他人的未提交工作带入 PR。

当多个功能经常需要集中观察、整批发布时，再通过单独决策引入 `dev`：功能分支合到 `dev`，整体验证后由 PR 合到 `main`，紧急主线修复同步回 `dev`。届时同步更新 PR 目标与 CI 触发分支。

流程参考 [GitHub flow](https://docs.github.com/en/get-started/using-github/github-flow)。维护者可在仓库 Settings 中将主线设置为必须经 PR 和离线检查；提交 workflow 文件本身不会开启分支保护，也不改变现有仓库权限。

## PR 说明与验证

使用仓库 PR 模板，写清问题、最终行为/使用方法、验证证据及尚未验证的范围。涉及状态或证据闸门时必须验证该拦住的输入被拦住；文档改动核对实际源码默认值、路径和可执行命令。

从活包根执行：

```powershell
uv sync --frozen
uv run --frozen python -X utf8 -m unittest discover -s tests -q
uv run --frozen python -X utf8 -m unittest discover -s doubao-jianying-orchestrator/tests -q
```

Windows 离线 CI 在 PR、`main` push 和手动触发时运行这两套测试及示例 JSON/CLI 检查。它使用锁定依赖，无生产密钥；不调用真实视觉/TTS 服务，不触碰生产队列、NAS 或飞书。AI 测试通过只证明程序合同，不能替代真实图片观察和人工成片验收。

每次改动更新 `docs/working.md`；可复用教训记入 `LESSONS.md`。默认模型写全为 GLM-5.3-Flash（`glm-5.3-flash`），流程默认 `ai_editor` / QC v4；旧行为注明 legacy 或历史适用范围。

## 私有配置与产物

本机配置放在忽略的 `work/`，公开模板位于活包 `examples/`。所有真实令牌、业务素材、抽帧、模型响应、审稿记录和日志保持私有。通用配置读取进程环境，不自动加载 `.env`。

提交前检查暂存 diff 和文件清单，确认没有真实密钥、机器专属路径或业务数据。不要修改第三方 `vendor/`；必要变更须另行说明来源、差异和验证。

## 发布与部署

日常功能/文档 PR 不改历史校验清单，不创建发布 tag，也不升级生产。

准备源码发布时，按[发布流程](docs/release-process.md)用 `tools.release_package build --checks full` 生成新清单、ZIP、全新解压测试与收据，再按既定真实任务观察/人工验收要求发布 tag 和 GitHub Release。附件版本、ZIP、哈希及文档状态必须一致。同步 README 下载入口与 `docs/RELEASES.md`，明确候选/预发布/正式状态。

实际安装另行备份、核对已安装技能和运行身份，再执行授权部署；合并 PR、打包和发布都不等于运行机器已经切换。
