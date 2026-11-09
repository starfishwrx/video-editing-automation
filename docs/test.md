# 测试策略

目标：让后续 agent 知道「什么算验证完成」，而不是只复制一条命令。

## 0. 日常按改动范围验证

单模块小改动默认跑对应模块及直接调用方；跨模块、公共合同、正式质量／交付门禁变化才跑完整两套。PR 的 CI 保持全量，发版在最终解压目录跑一次全量。本地已完成对应验证且代码没有再改时，不为提交或安装重复运行同一套全量；发版仍须验证全新解压包。安装后只做入口与依赖回读、必要的实际案例检查。

例如修改自动 BGM，执行以下定向回归（当前共35项）：

```powershell
uv run --frozen python -X utf8 -m unittest discover -s doubao-jianying-orchestrator/tests -p "test_*bgm.py" -q
uv run --frozen python -X utf8 -m unittest discover -s doubao-jianying-orchestrator/tests -p test_default_headless.py -q
uv run --frozen python -X utf8 -m tools.check_architecture
```

范围不确定或涉及共享接口时使用全量。失败后先定向验证修复；最终是否重跑全量由新增改动范围决定。

只检查源码字符串、文档数字、重复文件存在性且已有行为覆盖的用例可删除。优先验证实际调用、生成结果与应拦住的输入。动作／结果证据、完整口播、正式交付、NAS路径与恢复等测试继续保留，不为减少统计数删掉不同失败边界。

## 1. 完整离线回归（跨模块／门禁／发版）

在活包根执行（必须用 uv）：

```powershell
cd packages/video-v1.5.3
uv sync --frozen
uv run --frozen python -X utf8 -m unittest discover -s tests -q
uv run --frozen python -X utf8 -m unittest discover -s doubao-jianying-orchestrator/tests -q
uv run --frozen python -X utf8 -m tools.check_architecture
```

| 套件 | 覆盖什么 |
|---|---|
| `packages/video-v1.5.3/tests` | 包级工具：交付、NAS portable、合包、环境合同等 |
| `.../doubao-jianying-orchestrator/tests` | 引擎闸门、选镜、场景规则、时序、音画、CLI 合同 |

**通过标准**：两套 discover 退出码 0。不要用历史文档里的 `pytest packages/video-v1.5.3/...` 路径。

## 1.1 GitHub 离线 CI 与示例

`.github/workflows/ci.yml` 在 PR、`main` push 和手动触发时使用 Windows / Python 3.11、uv 与冻结锁文件运行上述两套测试，另核对 CLI `--help` 与 `examples/manifest.preview.json` 的 JSON 语法。CI 不注入生产密钥，不执行真实模型/TTS、NAS/飞书或生产部署。workflow 文件不等于仓库已开启必须通过检查的分支保护，维护者需在 Settings 中单独设置。

媒体测试前使用已有 `run.py setup-ffmpeg` 准备绿色 FFmpeg/ffprobe，并将路径传入 runner 后续步骤。依赖准备允许下载可执行文件；离线回归指不调用业务模型/服务，不表示 runner 已预装媒体工具。字幕 ASR 为显式注入，模型权重和真实任务数据不进 CI。

文档中的合成视频/`analyze-shots --no-vision` 示例见 `docs/quickstart.md`，可在准备 FFmpeg 后本地验证；它验证素材解析与抽帧，不作为视觉效果验收。模型、prompt 或选镜逻辑变更还需单独记录真实图片观察和人工审阅。

## 2. 定向回归（小改动默认）

模块边界检查：活包根运行 `uv run --frozen python -X utf8 -m tools.check_architecture`。`tests/test_modular_architecture.py` 验证旧/新导入共享状态、纯合同不加载引擎、中文空格路径副本资源、独立文件加载规则与反向依赖拒绝。`test_closed_loop_entrypoints.py` 覆盖两个旧入口、任意 cwd、importlib 独立命名空间、非 PASS、预览转正式和四项当前证据拦截；`test_queue_contracts.py` 与编排器 `test_media_boundary.py` 覆盖抽出的任务合同和媒体操作。新模块必须进入全新解压包后仍能运行。

| 改动面 | 可先跑 |
|---|---|
| BGM选曲／准备 | `test_*bgm.py` + `test_default_headless.py` |
| 无头建稿／字幕 | `test_default_headless.py`、`test_meeting_caption_integration.py`、`test_timeline_export.py` |
| 阶段恢复／环境检查 | `test_task_control.py`、`test_default_headless.py`；覆盖成功阶段串接、匹配提前失败、旧任务/文案/来源拒绝和env退出码 |
| 音频运行资源／干净拉取 | 编排器 `test_runtime_catalogs.py`、包级 `test_runtime_resources.py` 和 `test_release_version.py`；真实名称／倍速、缺表坏表阻断、Git 已跟踪文件或解压库存冷启动、私有缓存排除 |
| AI选镜 | `test_ai_editor*.py`、`test_selection_recovery.py`；至少一个具体卖点/未覆盖项、小窗重新观察、只改失败句；涉及卖点证据时加 `test_visual_qc_response_contract.py` |
| 选镜阶段续跑 | 包级 `test_selection_stage_resume.py`、`test_queue_contracts.py`；原配音/句长不变，输入变化、过程打断和预算耗尽不回到完整build，选镜后超时保留匹配 |
| 时间窗求解 | `test_timing_contract.py`；纸品动作另加 `test_paper_action_windows.py` |
| 草稿交付 / 媒体路径 | `test_draft_delivery.py` |
| 安装面诊断 | `test_delivery_runtime.py`；覆盖默认交付范围兼容、全库存同版本漂移、缺候选文件、坏库存及解析后路径逃逸；不加载生产模块 |
| NAS 往返 | `test_nas_portable.py` |
| 单一闸门 | 对应 `test_*gate*` / 模块测试，且必须含「该拦的输入被拦住」 |

普通源码 PR 依据适用离线回归、代码审查与目标 Windows CI 合并；真实成片、GUI 编辑及生产安装另行验收，不要求每次源码修复都先出两条成片。准备发版时才运行第 3 节。

## 2.1 实际媒体与端到端验收

独立时间线的固定输入开发入口见[程序化生产](programmatic-production.md)。`test_edit_timeline.py`、`test_production_timeline.py`、`test_timeline_render.py` 覆盖无草稿预检、重复句与多镜、实际音频和派生资产、CFR/非零 PTS、逐帧像素对应、保护末帧、原声偏移拒绝、字幕边界和失败修订。`tools.production_demo` 在新的包外目录生成三路音轨和可播放合成预览；检查总体保持 UNCERTIFIED，不取代真实商品内容与剪映可编辑性验收。

合成 FFmpeg 测试验证源帧/PTS、变速、结果帧、音频和字幕执行；缺少 FFmpeg 时的 skip 不代表媒体行为通过。准备绿色工具后需把其目录加入当前进程 PATH，并设置 FFMPEG_BIN/FFPROBE_BIN，再运行两套回归。

无头媒体修复的定向验证运行编排器 `test_headless_media.py`、`test_default_headless.py`、`test_ai_editor_qc.py` 和包级 `test_headless_media_coldstart.py`。媒体用例包含非整毫秒 48kHz 配音、旧格式和截断缓存、真实缺音拒绝、旧恢复来源迁移，以及 8/10bit 旋转后的解码像素/原声/帧时钟一致性。冷启动仅复制 Git 跟踪文件或解压库存，使用空用户目录的新解释器禁网执行真实 FFmpeg；CI 中只有外置无头后端被替代，不证明已安装的剪映或真实 native 草稿通过。

真实缓存恢复须另外在隔离输出目录通过正常 `recover --stage draft_write` 和已授权后端验证：保留配音、选镜与句长缓存，禁用新增 TTS/视觉请求，复用已处理 BGM，写出后读回 native 时间轴、媒体及源窗。缓存替代范围和未运行的独立视觉 QC 必须如实披露，不把草稿写入成功称为成片验收。

当前有 CLI、调度、草稿/交付夹具和合成媒体集成测试；真实商品从自动选镜到专业编辑接受的完整端到端质量，不由离线套件认证。纸品隔离验证保留已有失败，详情见[动作帧域窗口](paper-action-windows.md)和[回放说明](paper-selection-replay.md)。独立时间线方案的阶段验收见[实施方案](plans/20261101-programmatic-production.md)。

## 3. 发版验收（合包命令）

```powershell
cd packages/video-v1.5.3
uv run --frozen python -X utf8 -m tools.release_package build --output $releaseOutput --checks full
```

- `--checks delivery`：只跑交付相关，适合本机迭代。
- `--checks full`：异目录解压后跑完整两套测试，产出发布收据。
- 输出目录必须是**包外且尚不存在**的新路径；失败后换新目录，不要覆盖。

合包与库存校验均拒绝缺少必需音频表，即使重新生成库存也不能放行。冷启动测试不读取安装过的 Skill 或用户配置、不联网、不合成配音；它证明运行资源齐全及查询链可用，云端服务和真实素材质量仍需单独验证。

合包成功只证明源码包可重建；**不**等于渲染机已部署或生产验收通过。

## 4. 集成 / 真机（opt-in，不在默认 CI）

需要真实环境变量与素材，默认跳过：

- Windows 上运行真实任务，分别检查独立视觉 QC、剪映可编辑性和共享交付结果。
- 自动验收始终要求四项核心 PASS，无需观察收据或人工 prepare/collect。
- 凭据缺失必须有界失败，禁止硬编码兜底。

定时入口的离线回归覆盖防重入、重复触发、预览跳过、有限重试与任务间故障隔离；不会注册真实计划任务或调用真实视觉 API。

音频资源修复必须验证交付面：`tests.test_runtime_resources` 仅复制 Git 跟踪文件或解压包库存，在新解释器和空用户目录禁联网检查真实音色／倍速、自动选曲及下载 ID 调用链。`test_batch_dispatch` 用坏任务与好任务混批验证前置阻断、后续继续、运行记录完成及修正后恢复；覆盖名称后缀、独立倍速和 `voice_tempo_status`，不能只测未知音色字符串。`test_audio_catalog_admission` 覆盖暖缓存后的删表、坏表和修复，以及直接 build 在付费阶段前阻断。

## 5. 真机观察看什么 artifact

- `report/` 下选镜、场景规则、视觉 QC JSON。
- `last_result.json` / `task_log` 终态与未认证理由。
- 剪映打开草稿：媒体不丢、口播轨无异常气口、字幕在画布内、BGM 独立音轨（若启用）。
- 共享交付后：时间轴字面路径仍指向可读文件。

## 6. 改闸门时的强制项

任何放宽或新增闸门：

1. 增加「必拦样例」测试。
2. 不把 formal 降到 preview 语义。
3. 不在回复/PR 中声称「已上线」。

## 7. 纸品配音回归

新增 test_narration_preparation.py，使用真实PCM和FFmpeg覆盖连续边缘保护、末尾有声的内部停顿、五帧边界、时间轴缺口、静音/损坏、禁止过裁、精确tempo与源内容缓存身份、同资产并发、先定长后选择、最终写稿资产一致和冻结恢复。异常分类分别验证真实超限、工具缺失和TTS准备故障；非纸品保持原入口。test_paper_production.py继续覆盖最终闸门失败关闭。

门禁与共享音频入口变更必须跑两套完整discover和架构检查。冷启动的git-tracked-only测试要求新增模块已加入Git索引；测试台PowerShell输出编码须与测试的UTF-8解码一致，不以乱码失败判断音频行为。真实缓存隔离重放禁止新TTS/视觉请求，保持原音频与生产不变，核对预合成/引擎/写稿计划同字节；数值通过不代表听感或成片验收。
