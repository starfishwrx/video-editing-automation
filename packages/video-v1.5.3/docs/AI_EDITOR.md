# 卖点优先的 AI 选镜

规范性关键词见[要求用语与适用范围](../doubao-jianying-orchestrator/references/requirement_levels.md)。默认AI编辑、显式legacy和隔离预览分别适用。

纸品专项规则和参数见[纸品生产规则](PAPER_PRODUCTION_RULES.md)。AI政策升级为
`selling-points-first-reference-quality-v4`，计划、片段、草稿和独立QC记录生效规则版本；不要求规则内容哈希或收据自签。

正常 CLI 和任务模板默认使用 `ai_editor`。manifest 的 `selection_mode: legacy`
可显式回滚，其优先级高于 `JY_SELECTION_MODE`。未标模式的历史内存 matcher 输入
维持旧 API，不自动升级缓存。

## 业务标准

AI 通读整稿，按内容区分具体卖点、产品介绍、无具体卖点的钩子/衔接。
先给卖点安排真实画面，再给其他句子分配同商品包装、手持、拆包、取出和本体展示。
开场包含卖点时仍按卖点处理；普通钩子使用产品展示是正常匹配，不是降级。

轻薄、柔软、舒服允许贴切的真实展示，不强制实验或固定厚度对照。明确性能结果、
数量和数值比较须有对应画面；包装文字只支持该标称，不能代替功能演示。
具体卖点句MUST至少有一个具体卖点获实际画面支持，其他项MUST写入 `uncovered_selling_points`，MUST NOT宣称全部已经证明。零卖点支持MUST拒选。匹配先于去重，同一实验跨句顺接，不重播同一源窗口。
正式选镜和独立QC采用相同口径；显式 `allow_partial_selling_points: true` 仍是未认证预览开关，不可借此跳过独立QC。卫品等未配置纸品语义合同的任务，模型附带的纸品 `scene_ids` 只记录到忽略项，不作为支持证据；适用合同中的非法场景仍阻断。

## 执行流程

卫品吸水组合句须覆盖同一次实验的倒液、吸收、纸巾按压与结果，允许跨素材文件，禁止换成另一轮实验的验证。默认保留单源分相和同源原速连续句组；未覆盖的吸收/反渗组自动进入实际源帧配对、事件细化与局部变速。31/32等邻接仅影响召回；未知或无解不能正式放行。透气、荧光、促销和商品切换不得借入。配音、句长、字幕、BGM和组外画面保留；正式结果仍须实际草稿独立QC。显式`tools.weipin_experiment`保持预览用途，不能直接转正；其命令可复现定向人工观察。

默认实验组配对预算以`orchestrator/selection/experiment_groups.py`的`PAIR_LIMIT`为准，当前为6，MUST NOT超出同一作用域的预算。一次不同配对指一对候选镜头及各自源文件身份、时间窗，例如倒液候选A与按压验证候选B；不要求来自两个不同文件，也不表示最终要放6组实验或只调用6次模型。尝试记录MUST按句组、规则与采样协议作用域保存在`plan.json`的`experiment_attempts`，同一作用域续跑MUST沿用预算。原先3份的限制已由[ADR 0012](../../../docs/adr/0012-bounded-material-recovery.md)所述变更提高到6；同源原速连续组的每失败句最多3份组输入是另一项预算，不随之改变。

正常预览命令不依赖任务专属脚本：

```powershell
uv run --frozen python -X utf8 -m tools.weipin_experiment optimize --manifest work/task.json --timeline work/fixed/timeline.json --roles work/roles.json --evidence work/pair.pair.json --motion work/reviewed.motion.json --prior-origins work/origins.json --report-dir work/optimization-new
```

以`--phase-spec work/phase-spec.json`替换`--motion`可复现已定向复核的帧区间。用`phase-template --manifest work/task.json --evidence work/pair.pair.json --motion work/observed.motion.json --output work/phase-spec.json`生成带当前输入身份的模板，再依据实际编号帧修改`members[].observation`并填写`review_note`。不带`--motion`生成待填写模板。原件/采样映射或配对窗口变化后旧规格拒绝复用；模板本身不是视觉通过结论。

`optimize`输出独立时间线、逐句旧新源窗口/计划倍速、实际执行帧预算及相位滞后、工作预览和未认证检查结果，已有输出目录不覆盖；`--no-render`仅准备。多句组用重复`--sentence-id`明确选择。该入口不重做配音/选曲/全库识别，不回写NAS或业务表。

`cli._temporary_match → ai_editor.prepare → shot_analyzer.match_manifest`：

1. 描述只用于分类和宽召回，不用旧关键词、A/B强度和降级白名单淘汰。
2. 卖点候选每轮8个，重新提取带时间戳的8帧供GLM判断；不明确的候选最多加密
   重看一次。技术上无可行解或没有任何卖点支持时继续扩展。展示句共享产品展示池。
3. 单个候选调用失败保持unknown，保留其他完成的观察；只选已看清的候选。
   未审完或还有未知项时不能宣布缺素材，确定全库不支持的卖点单列缺口。
   首轮只有一个可行卖点候选时，最多多看一轮寻找剪辑备选；仍只有一个时保留原候选，不降低证据要求。
4. AI统一编排，可组合多镜。程序核验音频分配、源窗、证据覆盖和重复区间，
   技术失败最多交回修订两次，只合并失败句，已通过的其他句保持。具体卖点已匹配后的放置短窗复看继承原卖点结论，主要检查裁切完整性；普通展示仍重新审实际像素。每句最多三个不同窗口，原宽观察保留。此设计按用户要求维持，不在本次组接入中改为全量重判卖点。

原始长口播在预合成前仅按已有逗号、冒号拆成可读句段，禁止按字符硬切词语；
没有安全分隔点的长句继续阻断，显式传入的segments和legacy口径保持不变。
候选覆盖会区分证据未知、全库无支持、窗口无解；这些诊断均不自动等于正式通过。

调用共享现有视觉槽位，默认20；新模式等槽超时不绕过限流。调用默认180秒，
由`JY_AI_EDITOR_TIMEOUT_S`、其次`JY_VISION_TIMEOUT`覆盖；瞬态错误最多重试一次。
成功响应按提示词、实际图片、模型和服务地址绑定缓存。缓存及媒体证据保持私有。

选镜开始前保存 `selection_resume.json`，冻结当前输入、真实配音文件与句长。服务超时/限流保留 `VISION_CALL_FAILED` 和 `resume_stage=semantic_match`；认证失败、零支持和确定的窗口冲突不自动重试。worker 对每次原始运行最多执行一次持久化阶段恢复，复用已完成候选观察，仅补失败调用，再用原配音写稿。中断、输入或音频变化、预算耗尽均停止，不退回完整生产；显式新建稿才归档旧恢复状态。后续BGM/写稿超时不回退选镜。

## 新队列与AI编辑的规则边界

以下要求区分新队列、显式任务配置与历史冻结运行，不把新默认施加于旧恢复。配置和覆盖入口见[环境配置](../ENVIRONMENT.md)。

### 备注编号与冻结恢复

新队列转发`shot_notes`或配置的备注字段，编号依据默认`original_sentence`；显式`shot_notes_index_basis`仍MAY选择既有支持的`original_sentence`、`segment`或`split`。使用自然句编号时，MUST将原始自然句映射到经验证的口播分段；原文与口播分段整体不一致，或既有分段跨越自然句而使被编号备注的归属产生歧义时，MUST阻断并报告，MUST NOT挪动备注来掩盖这类分段歧义。越界编号或文本片段未命中的单条备注按既有合同记录到`shot_notes_unmatched`，不因此阻断整份分段映射。

新队列MUST冻结备注原始内容、编号依据及素材复用策略，重试MUST沿用既有绑定，MUST NOT因切句或编号默认值变化重新解释。显式强制重建时MUST先保留旧绑定再建立新快照。旧无版本原始manifest保留原分段编号，已有显式segments及旧冻结恢复沿用各自合同。备注内容仍是选镜提示，MUST NOT自动写入`visual_requirements`成为自由文本语义硬闸；它不替代画面证据。

### 成片素材复用

新队列冻结`source_reuse_policy=strict-v1`，未显式指定`max_source_reuse`时默认每个源最多使用2次。任务MAY以显式正整数覆盖该默认值，允许大于2；MUST NOT将2解释为不可提高的全局上限，也MUST NOT把旧无版本运行自动升级为strict-v1。该规则计一条成片中的源使用，不计模型调用或视觉缓存命中；同一源的两个不同物理切片通常计两次，经联合复核的连续组按实际涉及的每个源各计一次，预览缺素材占位不计。

strict-v1生效后，MUST按冻结的有效上限检查实际使用；局部补选MUST计入已保留的真实素材，SHOULD优先复核其他候选。没有合格替代且仍超限时MUST阻断，MUST NOT静默突破上限。显式提高上限不放宽既有源窗去重、动作完整性、画面证据或独立QC要求。维护源是`orchestrator/ai_editor_contract.py`及队列worker的默认值和覆盖逻辑。

### 显式任务速度上限

任务MAY配置`max_absorb_demo_speed`；配置后，默认AI编辑和AI补选MUST将其与共享播放政策取交集，并贯穿实验组阶段求解、每个物理切片、最终组装和两个写稿入口。因当前观察不能可靠排除吸水场景，显式上限保守适用于所有AI窗口，MUST NOT仅凭字段名称将非吸水画面豁免。未配置时MUST保留原政策，不新增默认限速。

新选镜收据MUST绑定生效上限，独立QC MUST按现有合同核对实际草稿源/目标时长、片段固定速度和对应速度素材；无法验证或不一致时MUST NOT认证。任务上限MUST NOT扩大完整短窗、纯过程或关键阶段各自的原许可。详细播放边界见[生产播放规则](../../../docs/production-playback.md#规则与生效点)。

### 可选排序观察

既有候选复看MAY同时返回信息价值及主体可读性观察；MUST只将有效结论用于原有证据检查已合格的候选排序。卖点判断MUST绑定原文，支持区间及可读性评价MUST限定于实际拟用源窗，MUST NOT把窗口外或通用展示观察提升为具体功效证据。

缺失或无效的可选观察MUST沿用原判断，MUST NOT仅因该可选字段缺失新增失败门禁；有效观察与原支持点矛盾时MUST保留不确定，MAY按既有预算加密复看。MUST NOT为填齐这些可选字段另开一条额外视觉调用流程，也MUST NOT用辅助排序替代最终独立QC。

### 补看窗口边界

存在有效`review_window_us`时，候选求解和最终组装MUST受其边界约束；continuation入点只能进一步收窄，MUST NOT重新打开未复看的原素材前缀。非法或越界局部窗MUST拒绝。旧观察没有局部窗口时MUST保留原范围，MUST NOT伪造局部复看记录。此条不取消已有完整短窗的卖点继承和各自播放许可。决策见[ADR 0012](../../../docs/adr/0012-bounded-material-recovery.md)。

### 混合补选与实验配对冲突

混合补选MUST保持保留规则句的原位置和原句序，MUST NOT将其认证成AI证据。实验组之外的素材复用冲突MUST NOT成为永久否定配对视觉观察的依据；按现有合同留给整稿修复时，整稿MUST解决全部冲突并通过最终证据、复用和时序检查后才可放行，MUST NOT因组内观察合格绕过最终校验。跨文件实验组锁定已接受邻句、普通连续组替换成员后重规划的边界保持不变。

## 素材准备的有界恢复

此节仅处理素材准备阶段缺少可审计描述、标签或置信度的回答，MUST NOT把所有视觉错误视为可排除项。单镜头额外重问由`JY_VISION_REASK_LIMIT`控制（默认2，范围0至2）；每产品可重问或排除的镜头总量MUST NOT超过`max(1, min(24, 镜头总数 // 100))`，已保留排除MUST计入总量。

重问后仍不可审计时，镜头MUST保持`pending_analysis`，同时记录`excluded_from_pool`及对应`shot_skipped_unauditable`警告，才MAY从候选池剔除，MUST NOT写成有效视觉缓存。任何就绪路径MUST保留至少一个非排除的有效镜头；禁用重问、超预算、传输或解析失败MUST继续阻断。被排除镜头MUST NOT用于选镜，最终当前草稿的独立QC与formal门禁不变。完整原因及恢复边界见[ADR 0012](../../../docs/adr/0012-bounded-material-recovery.md)。

## 接口与下游

- `ai_editor/plan.json`采用`chatcut-ai-selection-v1`，记录用途、卖点、候选观察、
  检查范围、编排和待审项；原响应、提示词与帧单独保存。
- 每段`ai_selection`采用`chatcut-ai-selection-evidence-v1`，绑定原文、段号、
  素材路径/大小/修改时间、源窗口与证据区间；多镜逐段记录，不要求拼图或提示词哈希。
- 末端语义检查与worker重验收据；引擎继续核验实际素材及最终窗口。
  新模式不用旧场景换镜器和词表裁决，不因多样性要求挤掉卖点画面。
- 引擎保存`ai_selection_identity.json`。独立QC核对实际草稿片段和源内容，
  从最终窗口重新提取开始、过程、结束及关键结果帧。
- 新QC合同为`chatcut-visual-qc-response-v5`，不接收选镜分数、理由和通过结论。
  DIRECT表示按本句用途正常匹配，不将体验展示声称为物理证明。旧v3只供legacy，
  不能认证新模式草稿；旧v4也须重新复核。worker/Eval重验原QC、草稿及素材身份。
- 失败、漏段、不确定、换镜后旧收据不能获得正式PASS。完整Windows流水线由 `scripts/run_pipeline.ps1` / `tools/workflow/scheduled.py` 执行；独立QC绑定当前实际草稿和源素材，实际剪映观察单独记录，目录或哈希检查不能代替画面。

## 验证及发布

合成回归覆盖误选、误拒、多卖点、钩子承接、扩展、连续性、回滚、盲审及窗口变化。
冻结新AI观察/计划后，`tools.sentence_review`可离线复放物化步骤，不读取媒体或调模型；
这不代表新提示词识别准确率。真实标签保持私有，调试与留出任务分开，未知项不补成正确项。

程序测试、真实GLM观察、独立QC、人工成片验收与生产安装分别留证。候选包/PR不表示
生产切换。GPU、任务队列和NAS分发配置不在本修订中更改。
# 变速与画面优先级

画面匹配先满足证据，再按 `policy/playback.py` 求解。完整放置短窗保留最高2倍许可；已复看的纯过程优先≤2、最多3倍。实验分相的关键动作、结果和计时证明原速；没有上述许可时原速。默认选镜、AI补选、最终求解、旧计划重验和写稿共用这些规则；旧8/20倍范围不能绕过。不改写素材事实，不清除观察/TTS缓存。

默认 `ai_editor` 保留相邻2–3句同源原速联合复核。卫品组另可使用 `reviewed-experiment-sentence-group-v1`，承载经配对、事件细化和阶段求解的单源或跨源片段。实验观察共用GLM/Qwen图片调用，不强制另配视频服务。反渗句起点对齐纸巾接触；关键首尾、提起和结果原速，明确过程局部加速。符合定性吸收语义时可尝试实际倒液尾段，省略前段明确记录。两种组都保留原配音字幕和组外内容，写前重验、写后回读并执行同组独立QC；失败不自动冒充正式通过。


宽召回模型若将 `source_###_shot_###` 缩写为 `source_###`，只有恰好对应一个真实镜头时才恢复完整编号；未知或歧义编号被记录并丢弃。召回排序不构成像素证据，仍需逐镜复核。
