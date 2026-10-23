# 人工反馈候选与句级选镜评估

本工具只收集私有审稿数据、复放确定性选镜，并报告指标。人工标签不参与生产选镜，也不能替代 formal、视觉 QC、Gold 或 Mac 验收。公开包仅含通用代码和合成测试；真实意见、源文件名、媒体、审稿人和截图均存入显式指定的私有执行目录。

## 反馈候选账本

`classify_feedback.py` 的 `category_guidance` 是当前分类的核对方向，`rule_text` 兼容字段为空。分类结果保留运营原文、方向标注、冲突和 `proposal_status=needs_review`。对“加粗/不要加粗”和“连续实验/无意义重复”分别保留方向，不能把同类意见直接合成一条新规则。

运行代理为 generic 反馈产出 `feedback_resolution.json`：

```json
{
  "task_id": "synthetic-task",
  "run_id": "synthetic-run",
  "issue_sha256": "运营原文 UTF-8 字节的 SHA256",
  "status": "resolved",
  "rule_key": "feedback.subtitle_style",
  "proposed_rule": "待人工验证的建议，不能冒充已发布规则",
  "repair": {"before": {}, "after": {}, "reason": "可审计的修复说明"},
  "evidence": [{"path": "实际私有证据文件", "sha256": "实际文件 SHA256"}]
}
```

`run_task.template.ps1` 在生成步骤后实际调用 `tools/feedback_candidates.py`。默认追加至运行时 `state/feedback_candidates.jsonl`，可用 `JY_FEEDBACK_CANDIDATE_LEDGER` 指定私有账本路径。旧 run、原意见漂移、证据哈希错误会留下 `FEEDBACK_CANDIDATE_PENDING` 日志，不会假装完成收集；收集失败不会改变生产验收结果。

每行记录 task/run/合同、原意见及哈希、resolution 和 QC/完成收据哈希、修复说明、建议规则与 `review_status=needs_review`。代理写入的 approved/reviewed_by 不会自动变成人工认可。相同收集重试不重复追加，修改意见产生新的候选，旧行不改写。并发锁或损坏行会阻断追加；确认为崩溃残留后由操作员处理锁，工具不会猜测并强制解锁。

状态仅以账本末尾的独立事件推进，要求人工 reviewer 和 SHA256 可核验证据；`regression_verified` 还要提供回归用例，`released` 还要提供 release 引用。查询当前状态：

```powershell
uv run --frozen python -X utf8 tools/feedback_candidates.py status --ledger $ledger --entry-id $entryId
uv run --frozen python -X utf8 tools/feedback_candidates.py transition --ledger $ledger --entry-id $entryId --status reviewed --reviewer $reviewer --evidence $evidenceJson
uv run --frozen python -X utf8 tools/feedback_candidates.py transition --ledger $ledger --entry-id $entryId --status regression_verified --reviewer $reviewer --evidence $evidenceJson --regression-case $caseId
uv run --frozen python -X utf8 tools/feedback_candidates.py transition --ledger $ledger --entry-id $entryId --status released --reviewer $reviewer --evidence $evidenceJson --release $releaseRef
```

候选进入回归、审核和发版仍走常规工程 PR：在私有审查记录中引用 `entry_id`、审核人、证据、回归用例和 release。收集命令不提供自动提升、更改版本或发布生产规则的能力。

## 句级审稿输入

`eval_stage.prepare` 生成的 Gold input 现在带可选 `sentence_reviews: []`。可以在该私有 JSON 内填写，或向 `eval_stage collect` 显式传 `--sentence-input`。两者同时非空会报歧义。外部输入必须绑定相同 `task_id/run_id/contract_hash/task_sha256/output_sha256`。

```json
{
  "task_id": "synthetic-task",
  "run_id": "synthetic-run",
  "contract_hash": "sha256:实际合同哈希",
  "task_sha256": "实际任务 JSON 的规范化 SHA256",
  "output_sha256": "实际草稿集合哈希",
  "sentence_reviews": [{
    "index": 1,
    "text": "该句运营原文",
    "context": {"before": "上一句", "after": "下一句"},
    "original_decision": {"shot_id": "old-candidate", "blocked": true},
    "split": "debug",
    "label_status": "reviewed",
    "reason_code": "false_block",
    "notes": "人工判断理由",
    "reviewed_by": "实际审核者",
    "evidence": [{"path": "实际审稿证据文件", "sha256": "实际文件 SHA256"}],
    "accepted": [{"shot_id": "candidate-a", "source_sha256": "合同内素材 SHA256", "source_window_us": {"start": 1000000, "duration": 3000000}}],
    "rejected": []
  }]
}
```

- 一个句子允许多个可接受候选/窗口，`accepted` 与 `rejected` 同源同候选的窗口不得重叠。窗口均为整数微秒，是审稿人允许的源区间；实际切片必须完整落在允许区间内才能计为接受。
- `reason_code` 可取 `wrong_selection/false_block/true_material_gap/needs_observation/acceptable`。只有审核过的记录可以带候选集合，且必须有审核人及真实证据哈希；没有人工结论写 `label_status=unknown`、空集合，不得借 Gold PASS 自动造标签。
- 素材哈希须属于当前合同。原脚本、上下文、原决策、理由、审核人和证据进入独立 JSONL，并同时保存在规范化 Gold 收据中。重复收集不重复追加。重复句号的多次不同意见不会自动投票或“最后一条覆盖”：评估前必须显式选出经仲裁的一套标签。
- `debug` 和 `holdout` 指标分别报告。留出案例应按任务或 SKU 隔离并在调参前冻结；字段名本身不能证明独立性，已被用于调参的历史集必须归为 debug。

正常收集入口，路径变量由操作环境提供：

```powershell
uv run --frozen python -X utf8 -m tools.eval_stage collect --task-dir $taskDir --draft-root $draftRoot --sentence-input $sentenceInput --sentence-ledger $sentenceLedger
```

如果仅收句级标签、不做 Gold/Mac 收集，可使用 `tools.sentence_review collect --input ... --contract ... --task ... --draft ... --ledger ...`。任务 JSON 必须与合同冻结摘要一致，标签文字须出现在该脚本中。多草稿重复 `--draft`；工具重算实际草稿集合哈希。两种入口均仅追加数据，不授予交付许可。

## 冻结和真实 matcher 离线对比

```powershell
uv run --frozen python -X utf8 -m tools.sentence_review freeze --contract $contract --manifest $manifest --analysis $analysis --semantic $historicalAi --timing $timing --draft $draftDir --output $frozen
uv run --frozen python -X utf8 -m tools.sentence_review replay --frozen $frozen --skill-root $baselineSkillRoot --output $baselineResult
uv run --frozen python -X utf8 -m tools.sentence_review replay --frozen $frozen --skill-root $currentSkillRoot --output $currentResult
uv run --frozen python -X utf8 -m tools.sentence_review compare --ledger $sentenceLedger --baseline $baselineResult --current $currentResult --output $comparison
```

冻结文件嵌入 manifest、观察、历史模型匹配与真实配音时长，绑定 task digest、合同和各输入文件哈希。复放调用显式指定包的真实 `match_manifest`，在独立 Python 子进程中、导入 matcher 前安装审计 guard，只允许临时目录写入，并阻断媒体读取、网络与再次启动子进程。包哈希包含 orchestrator Python 源码、`VERSION` 和机器规则 JSON；结果保留包哈希与冻结输入哈希。不调用生产编排或写生产账本。该范围不执行场景规划、渲染和成片验收，也不证明新提示词或新模型的准确率。

比较必须使用相同冻结输入。每句报告旧/新选择及接受、拒绝、已知可接受但阻断或 unknown，输出证据文件哈希：

- **Top1 接受率**：已接受 Top1 /（已接受 Top1 + 明确拒绝 Top1 + 已知有可接受候选却被阻断）。只有明确标注的单源窗口参与判定，部分标注范围以外的选择独立计为 unknown，不补成正例或负例。
- **误拒率**：有人工允许候选却被阻断 / 人工允许集合非空的句子数。
- 两项均输出分子、分母和 rate；分母为零时 rate 为 null。未标注句子列入 `unlabeled_result_rows/count`，未审、部分标注以外的选择和当前不支持整体评判的多镜组合单独计 unknown。没有真实标签时不报告任何真实人工接受率。

与运行端相同，这些统计不能绕过 formal gate。合成测试验证字段、阻断、分母与真实 matcher 调用链；成片节奏、完整动作与最终窗口仍须人工看片验收。
