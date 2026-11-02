# 卫品吸水实验：同源核验后再编排

吸水组合句必须选同一条源素材内的同一次倒液、吸收、纸巾按压、提起及纸巾结果。相同商品、相邻文件名或相似背景不能替代这一条件。透气、不闷热、荧光、商品切换及促销主题不并入吸水组。

当前实现是显式预览入口，未部署或切换默认队列。源观察与最终成片验收分别记录，任何输出继续保持待复核。

## 规则与证据如何进入程序

`weipin.json`保留原有文字要求，新增可消费的`experiment_contracts.absorb_rewet`。`ai_editor_contract.policy_prompt`同时加载纸品与卫品的有效规则，`purpose_review`明确卫品具体判据优先；观察协议与规则版本改变后，旧支持结论不能自动获得当前认证。

`purpose_production.select`复用旧索引描述作召回，但按当前规则重新编译原文。吸收与反渗交给单源实验组入口，不继续逐句塞完整实验。旧`render`拒绝过期规则身份、无效主题以及未经实验组装的功能句选择；没有合格实验时保留待处理，不用包装或另一条素材补成通过。

受控媒体准备从原件直接生成CFR60代理，再每6帧取一帧制成带编号的视频。保存原件、代理、视频、实际采样PTS和编号映射，并逐帧核对FFmpeg实际选择日志。该映射指向规范化代理的真实帧，不声称是原始VFR逐帧无损映射。旧的原件/代理关系缺少证明时，仅对选中的候选重建派生媒体，不能自动重识别全库。

千问读取实际视频并按可见编号返回动作证据。程序检查阶段齐全、顺序、同一实验连续性、素材身份和结果窗口。局部漏判可补查实际编号原帧；这类图片补查只记录部分事实，不把两个回答或两条素材拼成完整实验。

组装还会核对原文、商品、任务和规则身份；保留既有配音、句长、字幕及BGM。组外画面的原件/代理关系必须已知，来源未知就停止，避免另一份代理绕过同源重播检查。原件、代理、视频及元数据与输出路径发生碰撞时，在请求前拒绝。

当前业务组装器保护整段1倍速，结果最低15个输出帧只是防退化下限，仍需查看真实可读性。通用求解器原有2/3倍能力保留，但本入口尚未新增对可压缩过程的自动规划；不能据此声称已验证局部变速提升。

## 显式脚本入口

活包根目录运行，凭据与模型使用已有`JY_QWEN_*`进程配置。观察入口要求`JY_VISION_TRANSPORT=qwen`，服务不支持时明确失败，不静默换模型或回退拼图。

```powershell
uv run --frozen python -m tools.weipin_experiment roles --manifest work/task.json --timeline work/fixed/timeline.json --output work/roles.json
uv run --frozen python -m tools.weipin_experiment observe --manifest work/task.json --source work/material.mp4 --report-dir work/observation-new --font C:/Windows/Fonts/arial.ttf
uv run --frozen python -m tools.weipin_experiment detail --manifest work/task.json --evidence work/observation-new/observation.evidence.json --output work/detail-new.json --font C:/Windows/Fonts/arial.ttf
uv run --frozen python -m tools.weipin_experiment apply --manifest work/task.json --timeline work/fixed/timeline.json --roles work/roles.json --evidence work/observation-new/observation.evidence.json --prior-origins work/fixed/origin-bindings.json --output work/timeline-new.json --render-dir work/render-new
```

`prior-origins`由调用方从已有受控素材来源收据生成：每项含`asset_id`、原件与代理的真实`path/size/mtime_ns`，以及`clock: same_source_clock`。不能按文件名猜来源，不允许未建模的时钟偏移。输出目录必须是新的；该入口不写NAS、业务表或生产队列。

## 卫品42本轮结果

固定原13句1.2倍配音与现有音频。按主题只允许第8–10句组成实验组，共4.163719秒，排除第7句不闷热主题。复用92条旧描述，从已有功能召回复核9条候选；未确认一条同源完整实验。两条倒液素材缺纸巾按压结果，另外的按压素材缺倒液前提，剩余包含包装和硅胶球等误召回。

局部原帧补查找回两条候选中的按压、提起与纸巾结果，保留模型分歧与可见范围，未将其升级为完整实验。旧描述的补充召回没有提出新的合理候选，但这不能证明全库缺素材。

本轮没有产出更优的合格整片。已验证的改进是拒绝跨源拼实验、拒绝吞并透气主题、补查局部漏判和保留原输入；模型支持数不作为质量提升结论。先前041倒液加042按压及第6–10句借时的试验不满足原业务规则，相关提升结论已撤回。真实媒体、调用、拒绝原因与测试收据留包外。
