# 子系统：视觉匹配（visual-matching）

- 代码：`packages/video-v1.5.3/doubao-jianying-orchestrator/orchestrator/{visual_match.py, shot_analyzer.py, vision_analyzer.py, semantic_gate.py, draft_visual_qc.py, feedback_policy.py}`，以及 `prompts/semantic_analysis.md`

## 1. 职责
在“当前任务素材”范围内，生成可审计的候选镜头并逐句匹配脚本：
- `analyze_manifest`：场景切分、代表帧、候选清单（不写素材库，仅本任务）
- `enrich_with_vision`：按 contact sheet 批量调用视觉模型，产出描述/标签/动作确认/证据窗口（带缓存）
- `match_manifest`：逐句 Top3 与自动采用 Top1；直证优先、语义达标次之；候选窗口可行性求解；复用/相邻/机位相同闸门；需要时做多镜组合；preview 的黑屏/补镜；产出“可写稿”的 `matched_manifest`
- `draft_visual_qc.py`：生成成片后独立抽帧与二次视觉核验

## 2. 关键策略
- 候选池：来自当前任务素材（`material_sources/videos/segments`），上限 24；每源视频默认最多复用 2 次；相邻分镜“有替代则禁同源”
- 证据优先级：`DIRECT（有可见结果/动作） → ACCEPTABLE_DEGRADED（材质/结构类关联） → MATERIAL_GAP（只能补素材或改脚本）`
- 包装直证：当且仅当“手指/手势明确指向可读包装文字或标识”且脚本卖点一致，才记 `PACKAGING_CLAIM_DIRECT`，不代表真实性或吸收/柔软等功能已验证；显式低置信需独立高清文字和指向复核，非法置信不得认证
- CTA/数量：需多包、成排、整箱、堆叠或对应购买量感；单包的抽数/片数/PCS或1包不能当作囤货量感，非CTA规格句仍按同数字同单位核验可读文字
- 证据窗口：优先采用模型提供的时间段；缺少时按“审核帧邻域±w，夹在场景窗口内”，并作为证据来源写入
- 预览（preview）：允许保留直证/降级/黑屏并在“超 3 句黑屏”处做图文/展示类补镜；所有未解与缺口都仅记“未认证”，不得当作正式证据

## 3. 可行窗口求解与多镜组合（与时序契约同源）
- 对每个候选镜头用 `timing_contract.solve_window` 求解（真实音频时长 + 保护区 + 转场把手 + 证据覆盖 + 速度范围）；无解不再“交给引擎裁决”
- 单镜无解时，按“容量优先”拼多镜（`timing_contract.solve_span`）：分配严格等于 `音频+尾留白`，逐段再跑单镜求解；能解视作“正常采用”，不是降级
- 速度范围随 `visual_role`：与时间相关的直接证据锁 1.0；展示与场景允许小幅变速

## 4. 复用与多样性
- 复用硬约束：同一临时镜头单次；同源默认≤2 次；相邻分镜“有替代则禁同源”；“同机位 + 同阶段”时有替代也禁
- 多样性仅做同档候选的最后一级平手打散（换源/换机位/换阶段），绝不牺牲“卖点匹配/窗口可解/直证优先”

## 5. 产物（`match_manifest`）
- `segments[*]`：详见 orchestrator-engine 文档“输入/输出”
- `recommendations/top3`：候选摘要（分数/匹配卖点/可否采用/复用原因等）
- `reuse_counts/reuse_events`：复用计数与事件
- `degraded_matches[]`：降级采用的说明（匹配档、原因、窗口是否将就）
- `material_gaps[]`：按 claim 粒度的缺口清单
- 预览专属：`preview_unresolved[]`（单镜+多镜都无解但先摆镜头）；`visual_missing_matches[]`（留黑段位）

## 6. 后验 QC（`draft_visual_qc.py`）
- 只读打开草稿 → 用 FFmpeg 抓取“最终源窗口”的 mid/end 帧 → 按批次送视觉模型独立判断每句 `DIRECT/ACCEPTABLE_DEGRADED/MISMATCH/INSUFFICIENT_EVIDENCE`
- 合并 overall：`PASS` / `PASS_WITH_DEGRADED` / `PASS_WITH_GAPS` / `UNCERTIFIED` / `FAIL`；`UNCERTIFIED` 与 `DEGRADED` 不得自动升级为 PASS
- 当前响应合同为 `chatcut-visual-qc-response-v3`：逐句完整观察、原因、有效置信和索引覆盖均须核验；worker/Eval重读哈希绑定原报告并核对当前草稿身份，历史无版本报告不自动认证
- 换镜后以当前观察重新检查品类原子合同；未知判据进入 `contract_issues`，与当前输入实际缺素材的 `material_gaps` 分开，不能凭旧通过字段或旧参考片结论认证

## 7. 测试
- `test_visual_matching_governance.py`：直证/降级/否定/缓存命中/关联证据/相邻同源/同机位同阶段/预览黑屏与补镜等

## 8. 已知坑
- 句内无卖点关键词不再白送“精准档”，只按 AI 语义分判定（阈值 70）
- 证据区间必须逐段给，多镜时不能把“并集跨度”发给每段
- 旧分析结果缺字段时 `demo_actions.judge()` 走 `ungated`，覆盖率写进报告，不能当作“通过闸门”

