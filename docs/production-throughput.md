# 多任务生产调度与阶段恢复

当前实现以合格交付量和整批清空时间验收。离线回归验证取消、限流、缓存和恢复正确性，不代表已经测得生产加速倍数。

## 正常入口

从活包根运行，先把示例策略复制到私有配置目录，再设置所有入口共用的 `JY_CONCURRENCY_POLICY_FILE`。起始配置：制作4、媒体准备2、每任务API20、账户API20，请求360秒、QC阶段900秒、单任务14400秒。子进程不能用自己的环境变量扩大该策略。账户容量未知时保持20。

```powershell
uv sync --frozen
$env:JY_CONCURRENCY_POLICY_FILE = (Resolve-Path work/concurrency-policy.json).Path
uv run --frozen python -m tools.batch_dispatch run --queue work/batch_queue.json --dry-run
uv run --frozen python -m tools.batch_dispatch run --queue work/batch_queue.json
# 其他启动器提交到同一监督进程；空闲时接管监督职责。
uv run --frozen python -m tools.batch_dispatch submit --queue work/additional_queue.json
uv run --frozen python -m tools.batch_dispatch resume --record-id <record> --stage qc
```

`batch_worker.py` 的旧单条选择、重试/skip入口保留，但选完后提交到统一调度。内部 `process_task` 只接受已准入的 worker；旧脚本直接调用它会明确报错，需改用 `submit`。所有入口共享账户槽目录下的监督锁、持久提交文件和制作槽。

已有草稿的 QC 优先，然后运行已准备就绪的任务；新产品在独立准备池执行。同产品先把所需媒体准备完，再执行 API 池。等待共享缓存的任务不占制作名额；不同产品可以交错准备、请求和制作。同级 FIFO，提交任务按记录去重。

## 请求与缓存

`vision_client.post_chat` 独占 HTTP 重试责任。每个逻辑输入首次加一次重试，调用前记账，恢复只消费剩余次数；429/可恢复5xx遵守 Retry-After。总期限取消并关闭连接后才归还槽位。只有最终 `message.content` 能成为答案，思考、拒绝及截断 JSON 不作成功结果。

`thinking=enabled`、`reasoning_effort=low`、初始 `max_tokens=16384`。只有接入点兼容性确认后才启用 `JY_VISION_LENGTH_RETRY_VERIFIED=1`，允许一次32768预算重试。不能把所有空响应归因于思考预算。

公共缓存先于切镜/抽帧，按源文件 stat 和准备参数增量复用窗口、帧及拼图。视觉成功独立保存；旧缓存缺文字时只补两次文字复核，热缓存不再请求视觉或OCR。负观察保持不可匹配。候选8/16帧媒体跨任务共享，文案相关判断只复用完全相同的输入。

QC逐批保存已验证响应和当前最终切片输入，阶段超时保留草稿；恢复只补缺失批次。相同源与时间的既有QC帧也复用，源变化会失效。请求上限360秒，QC独立900秒，父进程额外保留30秒清理。补镜没有新候选、窗口或观察时停止整稿重建。

## 观察与校准

每轮进度重置，完整运行记录在 `work/dispatch_runs/`。请求账本及账户槽目录内的每日请求事件记录结束原因、token、等待与耗时、客户端在途采样，不保存密钥、图片正文或思考文本。统计只增加本轮新完成的、独立QC为PASS且已验证交付的草稿（不是已导出MP4）；预览、历史OK标记或失败子进程不能增加合格产量。

```powershell
uv run --frozen python -m tools.throughput_calibration --run work/dispatch_runs/<run>.json
# 仅在套餐账户容量已确认时比较下一档；工具自身不调用模型或改配置。
uv run --frozen python -m tools.throughput_calibration --run <run20.json> --run <run40.json> --confirmed-account-capacity 40
```

使用本来就要生产的任务分层比较缓存冷热、产品与文案。先固定账户20观察正确性/缓存收益，已确认额度后逐档20→40→80；固定API档位再比较媒体1/2/4。选择合格交付吞吐最高且失败率不恶化的观察，持平取较低并发。不要清空生产缓存或重复付费压测。

## 安装边界

源码目录仍是 `packages/video-v1.5.3`，实际版本读 `VERSION`。本次属于源码PR，不刷新历史发布清单。部署前先让现有批次排空，备份必要本机修订，再统一更新pipeline、skills及依赖；运行中不替换代码或策略。未启用的历史JyPoll队列继续保持关闭。真实成片、人工审核和生产吞吐需在安装后的正常任务中另行验收。

## 2026-10-28 优化候选

整体方案与PR取舍见 [生产优化方案](plans/20261028-production-optimization.md)。候选像素复审在同一句同一轮内采用就绪媒体/API独立池；不改变整稿编排顺序、8/16帧或共享账户额度。其他视觉阶段原有准备合同不变。

`verified_draft_deliveries` / `verified_drafts_per_hour` 为明确的草稿指标；`formal_deliveries` / `qualified_per_hour` 是兼容别名。`final_mp4_deliveries` / `final_mp4_per_hour` 保持null与not_measured，直到独立导出验收链接入。阶段统计含失败耗时，p50/p95注明样本数；并行耗时总和不是墙钟。material_wait包括准备准入等待；build阶段包括整个worker，不能解释成单一引擎子阶段。
