# 卖点优先的 AI 选镜

纸品专项规则和参数见[纸品生产规则](PAPER_PRODUCTION_RULES.md)。AI政策升级为
`selling-points-first-reference-quality-v3`，计划、片段、草稿和独立QC记录生效规则版本；不要求规则内容哈希或收据自签。

正常 CLI 和任务模板默认使用 `ai_editor`。manifest 的 `selection_mode: legacy`
可显式回滚，其优先级高于 `JY_SELECTION_MODE`。未标模式的历史内存 matcher 输入
维持旧 API，不自动升级缓存。

## 业务标准

AI 通读整稿，按内容区分具体卖点、产品介绍、无具体卖点的钩子/衔接。
先给卖点安排真实画面，再给其他句子分配同商品包装、手持、拆包、取出和本体展示。
开场包含卖点时仍按卖点处理；普通钩子使用产品展示是正常匹配，不是降级。

轻薄、柔软、舒服允许贴切的真实展示，不强制实验或固定厚度对照。明确性能结果、
数量和数值比较须有对应画面；包装文字只支持该标称，不能代替功能演示。
一句至少一个具体卖点有实际画面支持即可，其他项写入 `uncovered_selling_points`，不能宣称全部已经证明。零卖点支持仍拒选。匹配先于去重，同一实验跨句顺接，不重播同一源窗口。
正式选镜和独立QC采用相同口径；显式 `allow_partial_selling_points: true` 仍是未认证预览开关，不可借此跳过独立QC。卫品等未配置纸品语义合同的任务，模型附带的纸品 `scene_ids` 只记录到忽略项，不作为支持证据；适用合同中的非法场景仍阻断。

## 执行流程

卫品吸水组合句须覆盖同一次实验的倒液、吸收、纸巾按压与结果，允许跨素材文件，禁止换成另一轮实验的验证。31/32等相邻序号优先召回，再联合复核实际画面；相邻不是通过条件。透气/不闷热主题不并入吸水组。显式预览工具`python -m tools.weipin_experiment --help`提供`roles/observe/pair/motion/phase-template/optimize/detail/apply`；配对后按实际阶段证据局部变速，共享固定组预算，优先≤2倍、硬上限3倍，关键首尾/提起/结果原速。没有motion证据仍保守保护；不把该原速冲突当作允许局部变速也无解。自动边界观察仍不稳定，定向帧校正需如实标注，默认生产未切换，正式验收仍需真实成片独立QC。

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
   技术失败最多交回修订两次，只合并失败句，已通过的其他句保持。提议源窗超出旧证据时，对该小窗重新抽帧观察，每句最多三个不同窗口；复核不支持则记下拒绝，不裁剪旧证据获得通过。

原始长口播在预合成前仅按已有逗号、冒号拆成可读句段，禁止按字符硬切词语；
没有安全分隔点的长句继续阻断，显式传入的segments和legacy口径保持不变。
候选覆盖会区分证据未知、全库无支持、窗口无解；这些诊断均不自动等于正式通过。

调用共享现有视觉槽位，默认20；新模式等槽超时不绕过限流。调用默认180秒，
由`JY_AI_EDITOR_TIMEOUT_S`、其次`JY_VISION_TIMEOUT`覆盖；瞬态错误最多重试一次。
成功响应按提示词、实际图片、模型和服务地址绑定缓存。缓存及媒体证据保持私有。

选镜开始前保存 `selection_resume.json`，冻结当前输入、真实配音文件与句长。服务超时/限流保留 `VISION_CALL_FAILED` 和 `resume_stage=semantic_match`；认证失败、零支持和确定的窗口冲突不自动重试。worker 对每次原始运行最多执行一次持久化阶段恢复，复用已完成候选观察，仅补失败调用，再用原配音写稿。中断、输入或音频变化、预算耗尽均停止，不退回完整生产；显式新建稿才归档旧恢复状态。后续BGM/写稿超时不回退选镜。

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

画面匹配先满足完整证据，再按 `policy/playback.py` 的版本化生产政策求解。已复看的纯过程窗口优先≤2、最多3倍；关键动作、结果、计时证明及未知阶段保持原速。旧观察没有 `playback_observation` 时不授予加速权限。默认选镜、AI补选、最终求解、旧计划重验和写稿共用该规则；旧的8/20倍范围不能绕过。先尝试已有完整短候选，只改失败句；仍无解返回时长冲突，不改写素材事实，也不清除观察/TTS缓存。QC仍独立核对实际源区间和时间线，不能以选镜通过替代。自动跨句预算共享和旧宽窗相位拆分尚未接入。


宽召回模型若将 `source_###_shot_###` 缩写为 `source_###`，只有恰好对应一个真实镜头时才恢复完整编号；未知或歧义编号被记录并丢弃。召回排序不构成像素证据，仍需逐镜复核。
