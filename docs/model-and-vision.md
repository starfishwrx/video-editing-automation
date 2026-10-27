# 模型与视觉流程

适用范围：当前维护的 `packages/video-v1.5.3/`（实际版本读 `VERSION`）。实验包和历史发布包以各自代码为准。

## 型号与服务

**默认型号是 GLM-5.3-Flash，API ID 为 `glm-5.3-flash`。** GLM-5.3、GLM-5.3-Flash 和 ChatGLM3 是不同型号，文档、注释和验证记录都应保留完整名称。

智谱的[官方型号文档](https://docs.bigmodel.cn/cn/guide/models/vlm/glm-5.3-flash)列明 GLM-5.3-Flash 支持图像、视频、文本和文件输入。本项目目前通过图片拼图做视觉调用；它不把完整视频直接上传到模型。

- 服务商：火山方舟（Volcengine Ark）。
- 默认 base URL：`https://ark.cn-beijing.volces.com/api/plan/v3`。
- 实际请求：base URL 后拼接 `/chat/completions`。
- 图片入参：`messages[].content[]` 中的 `image_url`，其值为图片字节的 Base64 Data URL。
- 出参：模型生成文字/JSON；程序继续验证字段、时间范围、覆盖与身份，返回 JSON 本身不等于通过。

配置来自 [`vision_analyzer._vision_transport` 与 `_volc_settings`](../packages/video-v1.5.3/doubao-jianying-orchestrator/orchestrator/vision_analyzer.py)。源码默认值可以被进程环境覆盖；生产实际型号需结合该次配置及请求/响应收据核对。

## 每一步看到什么

1. `shot_analyzer.analyze_manifest`：FFmpeg 读视频、做场景切分并抽取代表帧；这一步不调用模型。
2. `vision_analyzer.enrich_with_vision`：把有编号/时间标记的帧拼成 contact sheet，发送图片与任务要求，获得描述、动作、结果和证据窗口，并保存缓存。
3. `ai_editor._inventory`：只读取上述描述和全稿文字，用于分类和候选召回；描述不作为已看清画面的证据。
4. `ai_editor._review`：对候选源窗口重新抽 8 帧，必要时加密到 16 帧，发送拼图逐项核对卖点。模型不能确认或调用失败时保留未知状态；AI 编排后由程序重验源内容、音频分配和窗口。
5. `draft_visual_qc`：从最终草稿使用的源窗口重新抽帧，以新调用进行视觉审稿。默认 AI 模式使用 v4；旧 v3 只供 legacy。独立 QC 不意味着自动换成另一个模型。

因此，同一个多模态模型可以承担“纯文字规划”和“看图检查”两种调用。某个调用只有文字输入，并不代表整个项目只使用文本模型。

抽帧属于采样观察：短动作、小字和遮挡仍可能看不清。程序收据、追加观察和人工验收分别保留，不将测试数量或模型自评分当作识别准确率。

## 配置与优先级

- 选镜模式：`manifest.selection_mode` → `JY_SELECTION_MODE` → `ai_editor`。
- transport：`JY_VISION_TRANSPORT`，空值默认为 `volc_glm`（别名 `glm` / `volc`）；它是调用通道名称，不是模型 ID。
- 模型：`JY_VISION_MODEL` → `glm-5.3-flash`。
- 凭据：`ARK_API_KEY` → `JY_VOLC_API_KEY`；两者都缺失返回 `VOLC_API_KEY_MISSING`。
- base URL：`JY_VOLC_BASE_URL` → 上面的 Ark 地址；填写 base URL 时不重复附加 `/chat/completions`。
- 默认直连的批次工作线程和跨进程槽位均为 20，分别由 `JY_VISION_BATCH_WORKERS`、`JY_VISION_SLOTS` 覆盖。
- 抽帧/拼图先以独立的媒体并发完成（`JY_VISION_PREP_WORKERS`、跨进程 `JY_VISION_PREP_SLOTS` 均默认 2），随后 API 按任务并发；文字复核也先拼图再请求，默认跟随批次并发，QC 使用同一批次并发配置。请求完成即校验保存，不等待最慢批次。
- 账户预算统一读取 `JY_CONCURRENCY_POLICY_FILE`；起点为制作4、媒体2、每任务API20、账户API20。没有策略文件时，旧环境变量只能收紧默认值。额度未确认前保持账户20；媒体并发独立校准。
- 默认思考模式启用；GLM-5.3-Flash 不支持关闭思考，不应设置 `JY_VISION_THINKING=disabled`。相关预算、超时和缓存变量见 [`ENVIRONMENT.md`](../packages/video-v1.5.3/ENVIRONMENT.md)。

通用配置从进程环境变量读取，没有通用 `.env` 自动加载器。PowerShell 示例见[配置模板](../packages/video-v1.5.3/examples/config.example.ps1)，本机副本和任务数据放在忽略的 `work/`。

`ai_editor` 当前要求 `volc_glm`；`codex` 和 `anthropic` 仅供显式 legacy 回滚。它们使用各自的模型/服务配置，不能套用 GLM-5.3-Flash 默认结论。旧函数 `match_with_codex` 的名称为兼容保留，实际 backend 仍取决于 transport。

## 如何留下验证证据

- 素材理解：`report/shot_candidates.json` 中的 `vision_analysis` 摘要和逐镜头观察。
- 新选镜：`report/ai_editor/plan.json`、调用旁的 `.request.json`，绑定提示词、图片、模型和 endpoint；相关媒体和收据保持私有。
- 最终复检：`draft_visual_qc.json`、草稿身份与源内容绑定；重跑不能以旧通过字段代替新输入的检查。
- 云端效果：真实模型观察和人工审阅单独记录。默认离线单测模拟网络，不证明云端识别效果或模型当前可用。
- 跨任务复用：同产品、同源文件及窗口的视觉观察和已完成文字复核共用持久缓存；任务 ID、文案和镜头编号变化不会触发重识别。同产品并发冷启动只由一个任务识别，其余等待缓存；旧任务缓存自动导入。已明确判为不适用的观察也复用，但不进入可用镜头池。配置与失效条件见环境合同。

修改默认模型或服务时，应同步本页、环境配置、快速开始和相关测试；先在隔离任务验证新服务的真实图片输入与返回合同，再考虑发布和生产切换。
