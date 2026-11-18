# Windows 商品视频自动化

把同一商品的原片和口播文案剪成可编辑的剪映草稿，配好逐句旁白、字幕和背景音乐。实验分支可把整张订单交给编排 Skill。Agent 先核对商品、文案和素材，再决定直接跑 Windows 初稿，还是根据实际切片补选、联动剪辑或恢复写稿。

| 手上的任务 | 从哪里开始 |
| --- | --- |
| 有素材和文案，要出第一稿 | 在实验分支把原订单交给 [整单剪辑 Skill](packages/video-v1.5.3/doubao-jianying-orchestrator/SKILL.md)。常规 Windows 队列见[快速开始](#快速开始)。 |
| 有草稿，要按意见换镜头 | 提供原稿、目标句和希望观众看见的内容。[已有草稿怎么改](#已有草稿怎么改) |
| 草稿已生成，质检或分发中断 | 用任务编号查最后完成的阶段，从对应步骤继续。[验收与恢复](#验收与恢复) |

## 快速开始

在 Windows 上准备 PowerShell 7、Git、Python 3.11 或 3.12 和 uv。以下命令先用仓库自带的虚构任务查看执行计划。

```powershell
git clone https://github.com/starfishwrx/video-editing-automation.git
cd video-editing-automation/packages/video-v1.5.3
uv sync --frozen

New-Item -ItemType Directory -Force work | Out-Null
if (-not (Test-Path work/config.local.ps1)) {
    Copy-Item examples/config.example.ps1 work/config.local.ps1
}
if (-not (Test-Path work/batch_queue.json)) {
    Copy-Item examples/queue.example.json work/batch_queue.json
}
pwsh -NoProfile -File scripts/run_pipeline.ps1 -RecordId example-product-a -DryRun
```

看到示例任务的状态 `PLANNED`，说明队列和入口能正常读入。后续命令均在上面的包目录运行。第一次配置机器也可以先用合成素材验证 FFmpeg 和 ffprobe 的抽帧结果。[离线示例与安装说明](docs/quickstart.md)

### 用自己的素材建稿

1. 在 `work/config.local.ps1` 配置 FFmpeg、ffprobe、单独授权的无头后端 `JIANYING_HEADLESS_ROOT`、兼容的剪映运行时 `JIANYING_INSTALL_DIR` 和静态字幕字体 `JY_HEADLESS_FONT`。让 `JY_DRAFT_ROOT` 指向独立草稿目录。默认通过火山方舟向 GLM-5.3-Flash 发送抽帧图片，凭据从本机环境的 `ARK_API_KEY` 或 `JY_VOLC_API_KEY` 读取。[环境配置](packages/video-v1.5.3/ENVIRONMENT.md) · [模型与视觉流程](docs/model-and-vision.md)
2. 在 `work/batch_queue.json` 填入任务编号 `record_id`、商品 `product`、规格 `sku`、口播 `script` 和素材目录 `material_dir`。音色按需指定，背景音乐留空时自动选曲。业务系统接单后，也把任务写进这份本地队列。[队列示例](packages/video-v1.5.3/examples/queue.example.json)
3. 加载配置并检查建稿环境。预检通过后，把最后一行的任务编号换成自己队列里的 `record_id`，即可调用配置的视觉和配音服务建稿。

```powershell
. ./work/config.local.ps1
New-Item -ItemType Directory -Force $env:JY_DRAFT_ROOT | Out-Null
uv run --frozen python -X utf8 doubao-jianying-orchestrator/run.py env
if ($LASTEXITCODE -ne 0) { throw '请先处理环境检查中列出的阻断项。' }
pwsh -NoProfile -File scripts/run_pipeline.ps1 -RecordId example-product-a
```

省略 `-RecordId` 会处理本轮符合条件的队列任务。手动和定时运行共用同一脚本。默认选镜策略 `ai_editor` 会按口播找画面，再生成原生草稿并做独立视觉质检。[Windows 流水线与定时运行](docs/windows-pipeline.md)

## 已有草稿怎么改

> 第六句又切回了开头的机位。换成能看清商品正面的镜头，保留原配音和其他句子。

把这类意见连同父稿、原匹配报告和音轨交给编排 Skill。它先连看目标句和前后镜头，再在本单素材里找能支撑原句的具体画面。普通展示句可用实验入口指定原片起止时间，在草稿副本上重切。前后句共用一个动作时，按确认的句子范围联动复剪。交回时标明改了哪几秒、保留了哪些音画、独立质检结果，以及仍需审片人判断的画面。[编排 Skill](packages/video-v1.5.3/doubao-jianying-orchestrator/SKILL.md)

一次历史试单里，审片人对两版单句修改都要求返工，随后认可了五句联动复剪的结果。独立质检仍认为其中一句缺少湿水画面的证据。整片首稿通过率的变化还需要同批对照验证。[实验方案](docs/plans/20261117-skill-refactor-experiment.md) · [试剪记录](docs/working.md#2026-11-18)

## 卫品吸水实验怎样算完整

成片里要按顺序看清**同一次实验**的三个阶段。

1. 倒液。看见液体倒到巾体上。
2. 吸收。看见倒入的液体被巾体吸收。
3. 纸巾验证。看见纸巾接触巾体，再提起来展示纸面是否沾液。

**三个阶段都允许按需加速，均无固定倍速上限。** 一句或几句口播都可承载整组。跨文件剪接时须从布景、巾体和液体状态确认属于同一次实验。独立质检按实际播放帧检查，缺段、快到看不清或混入另一场实验，都要重新选材。素材不足时报告缺哪一段。[卫品实验规则](docs/weipin-experiments.md) · [其他画面的播放规则](docs/production-playback.md)

## 验收与恢复

编辑稿在 `JY_DRAFT_ROOT`。查一条任务先打开 `work/<record_id>/done.json`，再看同目录的 `report/` 获取选镜和视觉质检记录，或看 `eval/` 获取交付验收结果。整批计划和跳过原因在 `work/pipeline_runs/`。

队列默认按 `preview` 出审片稿。正式交付需显式设置 `delivery_mode` 为 `formal`。当前输入、实际运行环境、草稿包和独立视觉质检四项均为 `PASS` 后，才按配置分发到 NAS、回填业务表。只在本机留稿可设置 `JY_SKIP_SHIP=1`。[验收标准](docs/test.md)

如已完成选镜和配音，从已保存的匹配报告和原音轨恢复写稿。已有草稿时，从失败的质检或交付阶段继续。运行记录标明可重试阶段和剩余次数。[状态与恢复操作](docs/windows-pipeline.md#状态与恢复)

## 版本与下载

当前维护源码在 `packages/video-v1.5.3/`，目录名沿用历史版本，包内 `VERSION` 当前为 **1.6.0**。下载附件以 GitHub Releases 为准，部署机器上实际启用的版本以 `ACTIVE_VIDEO_RUNTIME.json` 为准。[包内版本](packages/video-v1.5.3/VERSION) · [GitHub Releases](https://github.com/starfishwrx/video-editing-automation/releases) · [发布流程](docs/release-process.md)

仓库示例使用虚构数据。真实素材、密钥、模型响应和任务产物放在本机 `work/` 或其他私有目录。模型服务会收到任务相关的抽帧和文案，请在本机配置获准使用的服务。

开发或维护时，从文档索引按模块查找入口，提交前按改动范围运行对应回归。[文档索引](docs/README.md) · [维护指南](AGENTS.md) · [贡献约定](CONTRIBUTING.md)
