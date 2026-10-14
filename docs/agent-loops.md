# 建议的 /loop 任务

## Loop A：Issue / PR 分诊（每 30 分钟）
读 `AGENTS.md`。检查 starfishwrx/video-editing-automation 新的 issue 与 PR。
- Issue：能在 `packages/video-v1.5.1` 复现且属于代码缺陷 → 写最小失败测试 → 修 → 开 PR（不推 main）。涉及业务规则、凭据、生产数据、或需要剪映桌面/渲染机才能复现 → 标 `needs-human` 并说明原因，不动代码。低价值/重复 → 礼貌模板回复（先给我草稿，不直接发）。
- PR：本地 checkout 后跑 `pytest packages/video-v1.5.1/tests` 和 `.../doubao-jianying-orchestrator/tests`；检查是否改了历史版本目录、是否放宽 `RULES.md` 闸门、是否出现凭据或路径。只给评审意见，不合并。
- 新坑写进 `LESSONS.md`（同类 hits+1）。

## Loop B：生产反馈 → 修复 → 候选发版（每天 2 次）
读 `AGENTS.md` 与 `packages/video-v1.5.1/tools/classify_feedback.py` 的分类。收集过去 12 小时的失败回执（`failure_notification_receipt.json` / “问题反馈” 摘要，由人提供或放在约定目录，不直接访问飞书/NAS）。先按 `failure_class` 归类并丢弃环境类（凭据/路径/余额）。对可复现的语义/时序类问题：写回归测试 → 修 → 更新 CHANGELOG → 开 PR，标注“候选，需渲染机观察+人工验收”。永远不要声称已上线。

## Loop C：夜间质量巡检（每晚 23:00）
读 `AGENTS.md`、`LESSONS.md`。对 `packages/video-v1.5.1`：
1. 跑两套测试并记录数量（对照 `docs/RELEASES.md`，若数量下降要报告）。
2. 扫描：硬编码路径/凭据、浮点秒跨模块、新闸门缺测试、`engine.py` 中超过 150 行的函数。
3. 检查 `docs/subsystems/*.md` 与代码是否漂移（子命令、状态码、文件列表）。
4. 检查 `LESSONS.md` 中 `hits>=3` 且未 `PROMOTED` 的条目，起草升级 PR。
输出一份简报；每晚最多开 1 个“清理”PR，不做行为变更。

