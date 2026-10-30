# Windows 脚本流水线

整个项目按 Windows 脚本运行。定时任务启动同一个入口，读取本地任务队列，完成素材分析、选镜、配音、剪映草稿、独立视觉 QC 和配置的交付。Python 功能模块用于维护和测试，运行只调用本机程序与 GLM／千问 API。

```mermaid
flowchart LR
    A[Windows 计划任务或手动运行] --> B[run_pipeline.ps1]
    B --> C[scheduled: 选择本轮任务与限制重试]
    C --> D[dispatcher: 素材分析与制作]
    D --> E[worker: 草稿与独立视觉 QC]
    E --> F[Windows 四项验收]
    F --> G[formal 通过后按配置分发与回写]
    E --> H[预览或失败: 本地结果与原因]
```

## 运行一轮

从 `packages/video-v1.5.3/` 执行，先用 uv 建立环境，再复制已有配置模板。模型密钥通过进程或私有配置提供。

```powershell
uv sync --frozen
New-Item -ItemType Directory -Force work | Out-Null
Copy-Item examples/config.example.ps1 work/config.local.ps1
Copy-Item examples/queue.example.json work/batch_queue.json

# 在私有队列中填写真实素材和任务后，先查看本轮计划。
powershell -NoProfile -File scripts/run_pipeline.ps1 -DryRun

# 执行完整一轮。
powershell -NoProfile -File scripts/run_pipeline.ps1
```

配置副本保存在忽略的 `work/config.local.ps1`。已有私有配置不要用示例覆盖。默认 Python 为包内 `.venv/Scripts/python.exe`，可用 `-PythonPath` 指定；`-WorkRoot`、`-DraftRoot`、`-QueuePath` 和 `-ConfigPath` 接受绝对路径，支持中文和空格。默认队列是 `<WorkRoot>/batch_queue.json`，默认配置是 `<WorkRoot>/config.local.ps1`。显式参数优先。

队列是 JSON 任务列表，或 `{"tasks": [...]}`。沿用既有 worker 字段：`record_id`、`title`、`script`、`product`、`category`、`sku`、`material_dir`、`count`、`voice`、`bgm` 等。素材目录必须对运行账号可读，支持本机目录和已可访问的共享目录；脚本不依赖宿主私有的挂载辅助文件。任务获取方负责把任务写入此本地队列，本入口不自行拉取飞书任务。

`delivery_mode` 只能是 `preview` 或 `formal`，省略时按预览处理。预览保留本地草稿和未认证原因。正式模式通过验收后进入配置的 NAS／表格交付；本机留稿可设置 `JY_SKIP_SHIP=1`。NAS 与飞书凭据和路径见包内 `ENVIRONMENT.md`。

## 定时运行

下面的安装脚本把同一个完整入口注册到 Windows 任务计划程序，默认每 5 分钟一轮，可用 `-IntervalMinutes` 修改。运行身份为当前登录用户，使用隐藏窗口，重复触发时忽略并发实例；需要用户保持登录。安装脚本不会立即启动任务。

```powershell
powershell -NoProfile -File scripts/install_pipeline_task.ps1 -TaskName VideoAutomationPipeline -IntervalMinutes 5
```

安装脚本支持与运行脚本相同的路径参数。执行它会注册或更新指定名称的任务；源码更新和离线测试不会修改本机计划任务。历史 JyPoll/JyRun 入口维持退役状态。

## 状态与恢复

| 文件 | 用途 |
|---|---|
| `work/pipeline_runs/<run>.json` | 每轮任务计划、跳过原因、阶段结果和异常 |
| `work/pipeline_state.json` | 每条输入的制作与 QC 恢复次数 |
| `work/<record_id>/done.json` | 当前任务结果与交付状态 |
| `work/<record_id>/report/` | 分析、选镜、独立 QC 与失败证据 |
| `work/<record_id>/eval/` | 当前输入、草稿与自动验收结果 |

操作系统锁防止多进程重入。已完成、正在执行、普通预览和确定性失败会跳过；明确可重试的制作最多尝试两次，保留草稿的 QC／交付最多自动恢复两次，次数在开始前保存。恢复复用草稿，不重做 TTS 或选镜。同一记录的输入变化不能重置次数，必须使用新记录或显式定向恢复。不要删除整个状态文件来重放全部任务。

有明确修复后，可使用现有入口定向恢复：`python -m tools.batch_dispatch resume --queue <queue.json> --work-root <work> --record-id <id> --stage qc`。制作缺失才使用 `--stage build`，不会因定时器重复触发而无限重建。

## 自动验收边界

`tools/acceptance` 验证当前输入、运行时身份、草稿包和独立视觉 QC。四项都必须 PASS；正式报告必须明确来自 formal 构建并绑定当前草稿及实际源素材。超时、降级、缺失／非法模式和旧状态字符串均不能放行。NAS 分发、归档和表格成功回写之前都会检查，配置观察标签也不能绕过。

Mac 交接、人工 prepare/collect、Gold 对照放行和旧批准文件已退出运行链。旧合同不能自动转为新合同；缺少构建模式或源绑定的历史报告需要补本次独立 QC，不能手改字段伪造正式来源。离线句级反馈工具仍可用于维护规则，与自动交付无关。

运行时身份记录版本、路径库存与策略元数据，不证明源码字节未改或成片质量。离线测试和合成草稿验证同样不替代真实画面、声音与剪映可编辑性的观察。
