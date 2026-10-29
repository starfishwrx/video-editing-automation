# 生产吞吐优化方案：保留质量合同，减少重复工作

## 结论、基线与验收口径

不推倒重写。现有时间轴合同、剪映写稿、共享观察缓存、账户限流、独立 QC 和交付验证继续保留；把反复制作改为可度量、可恢复的阶段流水线。机器目标是 Windows、RTX 3070、十代 i5、32 GB；本文没有该机器生产实测，不承诺加速倍数或已达到 50 条/日。

本分支基于 `41df9905cf3e41cb7a293a97ffc5fa437d0271c4`（main，已含 #27）。维护目录仍为 `packages/video-v1.5.3`，真实版本读 VERSION；1.5.4/1.5.5 是隔离实验。生产安装须另核对 `ACTIVE_VIDEO_RUNTIME.json` 与实际代码，源码合入不是部署。

**当前调度的 formal_deliveries 是合格草稿交付数，不是 MP4 成片数。** 它验证独立源窗口 QC PASS、NAS 草稿及媒体收据。主生产路径没有接通并验收“导出 MP4 → 成片 QC”。vendor 的 GUI 导出接口注明仅支持剪映 6 及以下，不能据此认定当前剪映导出可用。本次明确报告 MP4 统计 `null/not_measured`，绝不把没有测量记成 0 或草稿数量。

| 工作窗口 | 50 条合格成片需要的吞吐 | 平均完成间隔 |
|---|---:|---:|
| 8 小时 | 6.25 条/小时 | 9.6 分钟 |
| 24 小时 | 2.083 条/小时 | 28.8 分钟 |

这是整条生产线输出节拍，不是并行任务单条延迟上限。按 80% 有效时间，串行资源预算为 7.68 / 23.04 分钟每条；如一次通过率 80%，约需处理 62.5 个候选才能获得 50 个合格输出（粗略计划，不是实测）。GUI 导出、人工审阅及返工也须计入。

## 十月 PR 回顾与取舍

检查了仓库 #1–#28 的正文、合并/关闭状态，重点检查 #19–#28 的逐行审查及当前代码；历史 PR 的自报测试不是本次重新验证。快照时间为 2026-10-28；发布前复查main仍为41df9905，#28 head已更新为2e7d76a7（任务身份/修订反馈），仍不纳入本分支。远端状态可继续变化。

| PR | 状态（审查时） | 与本次关系 |
|---|---|---|
| [#1](https://github.com/starfishwrx/video-editing-automation/pull/1) | 已合并 | 初始 Agent/架构文档，不另造入口 |
| [#2](https://github.com/starfishwrx/video-editing-automation/pull/2)、[#3](https://github.com/starfishwrx/video-editing-automation/pull/3) | 已合并 | 草稿可搬运与正式证据闸门必须保留 |
| [#4](https://github.com/starfishwrx/video-editing-automation/pull/4) | 已合并 | 证据窗口与 20 倍边界；本次不改速度语义 |
| [#5](https://github.com/starfishwrx/video-editing-automation/pull/5) | 已合并 | 1.5.4/1.5.5 索引实验，不混为当前生产默认 |
| [#6](https://github.com/starfishwrx/video-editing-automation/pull/6) | 关闭未合并 | 旧 10 槽提案；不复活 |
| [#7](https://github.com/starfishwrx/video-editing-automation/pull/7) | 已合并 | GLM-5.3-Flash / GPU / 20 槽默认，不盲目扩额度 |
| [#8](https://github.com/starfishwrx/video-editing-automation/pull/8)、[#9](https://github.com/starfishwrx/video-editing-automation/pull/9) | 已合并 | 证据优先、独立QC、NAS与人工审稿合同继续生效 |
| [#10](https://github.com/starfishwrx/video-editing-automation/pull/10)、[#11](https://github.com/starfishwrx/video-editing-automation/pull/11)、[#12](https://github.com/starfishwrx/video-editing-automation/pull/12) | 已合并 | 气口、字幕单位、历史发布清单；不改历史清单 |
| [#13](https://github.com/starfishwrx/video-editing-automation/pull/13) | 已合并 | AI候选像素审查、整稿编排、QC v4；本次只优化准备与调度 |
| [#14](https://github.com/starfishwrx/video-editing-automation/pull/14)、[#16](https://github.com/starfishwrx/video-editing-automation/pull/16)、[#17](https://github.com/starfishwrx/video-editing-automation/pull/17) | 已合并 | 活目录路由、Windows CI、会议方案；沿用 |
| [#15](https://github.com/starfishwrx/video-editing-automation/pull/15)、[#18](https://github.com/starfishwrx/video-editing-automation/pull/18)、[#21](https://github.com/starfishwrx/video-editing-automation/pull/21)、[#22](https://github.com/starfishwrx/video-editing-automation/pull/22) | 已整合合并 | 以 #22 最终行为为准；不恢复 ASR 或多层哈希门槛。保持媒体/API独立池，但候选就绪即可调用，不再等整个候选轮准备完 |
| [#19](https://github.com/starfishwrx/video-editing-automation/pull/19)、[#20](https://github.com/starfishwrx/video-editing-automation/pull/20) | 已合并 | 批量抽帧已有，不重复造；修其从头解码与零时刻/邻近/VFR时间映射，接入候选8/16帧 |
| [#23](https://github.com/starfishwrx/video-editing-automation/pull/23)、[#25](https://github.com/starfishwrx/video-editing-automation/pull/25) | 已合并 | 产品缓存、统一调度、重试账本、QC恢复已有；本次增量扩展，不重新实施 |
| [#24](https://github.com/starfishwrx/video-editing-automation/pull/24) | 关闭未合并 | 不引入把功能证据失败软改为通过的覆盖逻辑，也不引入收据自签门槛 |
| [#26](https://github.com/starfishwrx/video-editing-automation/pull/26) | 开放 | 品类别名更正，独立处理；本分支不覆盖规则JSON |
| [#27](https://github.com/starfishwrx/video-editing-automation/pull/27) | 已合并 | 已作为基线。补审查指出的部分预览转正式漏洞、旧缓存相对路径；保留其选镜、密集复看、文字恢复与字幕位置 |
| [#28](https://github.com/starfishwrx/video-editing-automation/pull/28) | 开放 | 语义分句/规则优先独立候选；不合并其默认策略。其 shot_analyzer 差异在语义裁切而非抽帧，engine/文档仍需合入时正常解决相邻差异 |

#28 的索引备注分句、已冻结计划在新目录保存等审查项不由本分支静默移植。不要用“优化”掩盖不同选镜策略的效果对比。

## 目标阶段结构

```text
任务与运行版本冻结
  ├─ 商品素材增量入库：探测/镜头/帧/共享事实观察（可恢复）
  └─ 定稿文案与音色 → TTS真实时长
          ↓
候选召回 → 有界就绪队列 → 像素适配审查
          ↓
整稿编排 → 确定性窗口/证据/音频合同
          ↓
独立复制一次到草稿媒体 → 写稿与结构校验
          ↓
独立源窗口QC（批次检查点）→ 只修失败片段
          ↓
待实现/真机验证：单GUI导出worker → MP4技术/视听QC
          ↓
按既有要求人工审核 → 正式成片交付与计数
```

单机模块化队列足够，不引入微服务或新的分布式基础设施。语义审查与编排保持原有约束；不以降低采样、降低分辨率、取消独立 QC 或放宽 formal 换速度。

## 本 PR 实施范围

### 1. 有界准确抽帧

复用 `extract_frames_batch`：从目标附近 seek，限定解码结束区间，showinfo 相对 PTS 还原到源时间；零时刻、重复/相邻请求可映射到同一实际帧，不凭输出序号硬猜时间。无法证明时间对应时保守精确单帧回退。临时目录唯一并在失败/超时/早返回后清理。

候选 `_review_media` 使用同一批量方法，8帧/16帧、窗口、拼图和源身份检查不减。采样合同未改，既有有效媒体缓存继续复用；实际观察调用依旧按完整目的输入缓存。

### 2. 就绪候选调度

`ready_work.iter_ready` 将准备池和API池分开；最多准入 `prep_workers + api_workers` 个工作项。某候选已准备好即可调用，不必等待慢候选。准备失败不进入模型，返回未确定。统一账户API槽与期限仍由现有 transport 控制。

只在同一句、同一候选轮内调整执行顺序。句间顺序、有限扩展、稀缺卖点、密集与保护区复审、最终整稿计划都不改。结果按候选ID回填而非完成顺序；成功调用仍沿现有缓存与检查点恢复。密集/保护区准备失败保留原有 `retained_original` / `unconfirmed` 语义，不把失败认证为新证据。

### 3. 去掉中间媒体复制

容量检查在原素材上完成，记录源stat；写草稿时再独立复制一次到最终草稿media。入场后或复制中源文件变化会阻断，复制中断不发布半文件。使用源身份确定性文件名隔离并发同名素材；缓存复用校验目标实际stat，同长度被编辑的草稿媒体不会直接复用。

不使用硬链接/软链接，不让草稿依赖report/uploads，原素材删除后草稿副本仍可读。NAS官方收集、原稿保护、共享路径与运输校验不改。stat是缓存/并发一致性判断，不是新增成片质量证书。

### 4. 精确吞吐观测

保留 `formal_deliveries` / `qualified_per_hour` 兼容字段，同时明确 `verified_draft_deliveries` / `verified_drafts_per_hour`；最终MP4计数/速率为 `null`，状态 `not_measured`。

调度记录material/build/qc阶段成功及失败耗时；统计样本数、总耗时、p50/p95（nearest-rank）。并行阶段总耗时不可当墙钟时间。`build`是worker整段，不是引擎内部单子阶段。等待拆分为material等待和worker等待；前者包含准备槽排队，不等于CPU耗时。新增提交从submitted_at算起，旧提交从监督器准入算起，明确标注来源。

### 5. 恢复与质量边界修复

旧观察导入用各自manifest父目录解析相对素材路径，与正常素材入口一致；仍验证商品/旧任务身份、源范围、观察协议，不提升未复核文字。

部分预览收据保留完整卖点需求；仅显式preview+允许部分覆盖可构建未认证预览。formal及缺少preview上下文的验证拒绝未覆盖需求，也拒绝历史已经截掉需求但仍标记uncovered的预览，防止复用预览绕过正式闸门。

## 后续顺序与停止条件

1. **安装与基线**：现有任务排空后备份并统一更新pipeline/skills；确认实际源码、Python/ffmpeg/剪映/API配置。非本PR自动执行。
2. **正常任务观察**：同商品多文案与新商品分别记录冷热缓存。不清空生产缓存，不为压测重复付费生成。
3. **资源调参**：先固定已确认API额度，比较媒体1/2/4；再固定媒体设置，只有账户额度明确时才上调API。胜出条件是合格输出吞吐增加、误放与失败率不恶化，持平取较低并发。
4. **局部计划恢复**：已有请求/素材/QC缓存保留；未来将整稿计划分阶段持久化、仅失效受文案变化影响的片段。必须继续做全稿重复/衔接约束，不在本PR把语境从prompt删除来追求缓存命中。
5. **导出闭环**：确认当前剪映GUI接口、实际可用导出权限与版本，建立单导出worker及失败恢复，然后验证最终MP4字幕、音频、黑帧、时长、分辨率和人工审阅收据。本云端不能声称已验证Windows GUI；完成之前50条指标只能分别报告草稿与成片，不得混算。

质量评估采用固定真实样本人工盲评：误放、误拦、一次通过率、返工分钟和最终合格输出，而非只看单测数量或模型自评分。独立QC默认使用同一视觉后端，不意味着统计上无相关误判。

## 本次验证记录

- 冻结依赖、离线合成媒体与mock API；未调用付费模型/TTS、未操作真实业务素材、NAS、飞书、生产队列或GUI。
- 真实CPU FFmpeg：CFR/VFR、非零容器start_time、晚窗口、零/重复/邻近时间戳像素对照；不外推RTX3070生产加速倍数。
- 对照基线41df9905在相同Linux环境跑两套全量测试，原有8项平台路径失败（包级4、编排器4）均记录；最终新增变化另跑目标测试，目标Windows CI须对最终PR head单独核验。
- 最终Linux全量：包级126项，3失败/1错误/1跳过；编排器815项，2失败/2错误/1跳过。与base包级120、编排器792相比，失败名称与原因一致，无新增失败；不是全绿。新增与受影响目标套件及独立复核通过。编译/CLI help/diff检查通过。Windows CI结果以PR检查页为准；没有完成生产安装或人工成片验收。
