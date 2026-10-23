# 本地测试输入与画面审核

`tools.v154_pilot` 在新目录冻结素材、已有观察、逐句真实音频和显式 BGM。
冻结时核对源文件前后元数据和副本 SHA256。每个 A/B arm 生成前重新校验，
两边共享相同输入身份。历史人工挑选的 `contextual_plan` 不导入新任务，
历史 B 片不作为当前自动规划结果。

所有机器路径由参数提供。验证产物留在包外的新目录；不写 NAS、业务表、
生产队列或全局剪映草稿索引。两个 arm 分别使用新建本地 drafts、cache、
usage 和日志。以下命令在包根运行，依赖使用已锁定的 UV 环境。

```powershell
uv run --frozen python -m tools.v154_pilot freeze --input <原manifest> --observations <原观察JSON> --audio-segments <可选逐句音频JSON> --bgm <固定本地音乐> --output <新冻结目录>
uv run --frozen python -m tools.v154_pilot prepare-arm --frozen-root <冻结目录> --package-root <基线包根> --strategy legacy --output <新A目录>
uv run --frozen python -m tools.v154_pilot prepare-arm --frozen-root <冻结目录> --package-root <候选包根> --strategy indexed_contextual --index-root <实验共享索引目录> --model-cache-root <包外模型目录> --output <新B目录>
uv run --frozen python -m tools.v154_pilot run-arm --arm-root <A或B目录>
```

准备 arm 不运行模型。`run-arm` 才会调用包内正常 `run.py build`；不提供另一套
写稿器，也不手动修改草稿 JSON。全段采用文件音频，保留历史音色名称和来源，
不静默换音色或重新合成。

`tools.v154_review render` 从正常构建后保存的草稿读取实际音视频和字幕轨，
生成 720×1280 审核副本并记录草稿、时间线、视频 hash 和实际编码器。
只支持当前项目的单视频轨硬切、等速片段、固定亮度和水平翻转。
空隙、越界、不支持的变换和读不到的素材会停止渲染；不填黑屏、不冻结尾帧。
审核渲染不等于剪映桌面回放或正式交付验收。

```powershell
uv run --frozen python -m tools.v154_review render --draft <已保存草稿目录> --package-root <包根> --output <新审核片.mp4>
uv run --frozen python -m tools.v154_review collect --manifest <冻结manifest> --arm-root <A目录> --arm-root <B目录> --cases <真实案例JSON> --output <审核JSON>
uv run --frozen python -m tools.v154_review html --input <review_document.json> --output <review.html>
uv run --frozen python -m tools.v154_review validate-feedback --review <review_document.json> --feedback <下载的反馈JSON>
```

审核文档的每个 arm 保存实际 plan、选镜理由、状态、成片地址和源内容哈希。
页面可预览实际切片的原片区间、逐例标注和导出反馈 JSON，绑定 review、plan、
case 和 source 哈希；不写人工认证台账。`collect` 只收集当前 arm 的实际输出，
缺片或构建失败会保留其状态，不搜索历史片替代。

首轮固定两条任务，每条各一条 A 和一条 B。另冻结 30 条真实正反候选，覆盖
拆封、抽取、吸水、透光、擦拭、包装和相反语义。案例必须引用真实视觉描述、
动作区间或人工记录，文件夹名不作为真值。历史模型观察标记为
`historical_model_observation`，人工尚未确认时 `truth_status=PENDING_HUMAN_REVIEW`；
不得以这些待审标签宣称已达到 precision/recall，也不得以旧 B 人审结果冒充新 B 验收。

发布收据分别记录程序测试、包校验、实际画面检查、人工接受和生产部署。
无法构建的 arm 保留具体失败结果并展示缺口，不用历史片补齐 A/B。
