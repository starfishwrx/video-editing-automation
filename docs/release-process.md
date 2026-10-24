# 发版流程（v1.5 系列）

适用范围：`packages/video-v1.5.x/` 候选包的“源代码包”发布与归档（不包含渲染机/生产验收）。

当前维护主线固定在 `packages/video-v1.5.3/`，版本读 `VERSION`；1.5.4/1.5.5 是隔离索引实验。日常 PR 不刷新历史清单、不发 tag。准备发布时重新构建、验收，再将对应 tag、ZIP、校验文件和发布说明关联到 GitHub Release；同步根 README 的下载入口及 `docs/RELEASES.md`。发布附件和生产部署状态分别说明。

## 当前活包可执行入口（目录 video-v1.5.3，VERSION 见包内）

已验证修复的合包技能随包提供：
[`validated_fix_to_release.md`](../packages/video-v1.5.3/doubao-jianying-orchestrator/references/validated_fix_to_release.md)。
从 `packages/video-v1.5.3/` 运行：

```powershell
uv sync --frozen
# 局部交付迭代：相关回归；输出必须是包外的新目录
uv run --frozen python -X utf8 -m tools.release_package build --output $deliveryOutput --checks delivery
# 发版 PR：全新解压副本运行两套完整测试
uv run --frozen python -X utf8 -m tools.release_package build --output $releaseOutput --checks full
```

命令生成 ZIP、SHA256SUMS、逐文件校验、测试日志和发布收据，不部署、不推送、不开队列。
`verify --package $unpackedPackage` 可只读核验已有包；清单漏项、损坏或版本不一致会失败。
已有输出目录不覆盖；失败后先查日志，使用新的输出目录再验证。
以下为各历史版本通用说明；当前包不再使用下述手工 PowerShell 清单命令。

前置条件
- 两套测试全绿：见 docs/test.md（uv + unittest；当前活包 packages/video-v1.5.3）
- 规则版本与运行模式已复核：`RULES.md`（视觉规则 1.4.1）、preview 语义与未认证标注一致
- 变更说明：更新 `doubao-jianying-orchestrator/CHANGELOG.md`（若有）

步骤
1) 校验并冻结包内容
- 在 `packages/video-v1.5.x/` 下生成/更新轻量内容收据：
  - 执行（示例）：
    - Windows PowerShell：`Get-ChildItem -Recurse | Get-FileHash -Algorithm SHA256 | ForEach-Object { '{0}  ./{1}' -f $_.Hash, $_.Path } | Set-Content -Encoding UTF8 PACKAGE_CONTENTS.sha256`
    - 或使用现有脚本/工具生成，确保编码为 UTF-8（可带 BOM）
- 确认存在并更新：`VERSION`、`release-manifest.json`（列出来源、文件清单、ZIP 构建方式）

2) 版本记录与归档
- 在仓库 `docs/RELEASES.md` 中追加或修订对应条目（来源、文件数、测试数量、ZIP SHA-256、状态）
- 在 `releases/` 增加对应 `SHA256SUMS-video-v1.5.x.txt`（如需），并在 GitHub Release 附上 ZIP 与校验文件

3) 交付与生产验收（不在源码包 PR 中完成）
- 源码包通过测试 ≠ 生产通过。部署到渲染机/Windows 工作器前：
  - 从 ZIP 全新解压；核对 SHA-256 与 `PACKAGE_CONTENTS.sha256`
  - 运行包级测试与编排器测试
  - 执行两条“真实任务观察”（不同脚本/素材组合），并经人工审阅/Mac 审核通过
- 成功交付的判据：`closed_loop.audit` 满足策略（`QC/PASS`、Mac 审阅绑定、打包完整）

注意
- 不得修改历史版本目录（v1.5.2 及更早）；如需修文档，在仓库 docs/ 层新增说明
- 仅在“发版 PR”中更新 `PACKAGE_CONTENTS.sha256`；日常功能 PR 不动该文件
- 生产凭据与路径不进入仓库；所有路径/凭据来自环境变量（见 `ENVIRONMENT.md` 与 `platform_env.environment_report()`）

