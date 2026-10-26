# 测试策略

目标：让后续 agent 知道「什么算验证完成」，而不是只复制一条命令。

## 1. 默认离线回归（每次功能 PR）

在活包根执行（必须用 uv）：

```powershell
cd packages/video-v1.5.3
uv sync --frozen
uv run --frozen python -X utf8 -m unittest discover -s tests -q
uv run --frozen python -X utf8 -m unittest discover -s doubao-jianying-orchestrator/tests -q
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

## 2. 局部迭代（可缩小）

| 改动面 | 可先跑 |
|---|---|
| 草稿交付 / 媒体路径 | `test_draft_delivery.py` |
| NAS 往返 | `test_nas_portable.py` |
| 单一闸门 | 对应 `test_*gate*` / 模块测试，且必须含「该拦的输入被拦住」 |

普通源码 PR 依据适用离线回归、代码审查与目标 Windows CI 合并；真实成片、GUI 编辑及生产安装另行验收，不要求每次源码修复都先出两条成片。准备发版时才运行第 3 节。

## 3. 发版验收（合包命令）

```powershell
cd packages/video-v1.5.3
uv run --frozen python -X utf8 -m tools.release_package build --output $releaseOutput --checks full
```

- `--checks delivery`：只跑交付相关，适合本机迭代。
- `--checks full`：异目录解压后跑完整两套测试，产出发布收据。
- 输出目录必须是**包外且尚不存在**的新路径；失败后换新目录，不要覆盖。

合包成功只证明源码包可重建；**不**等于渲染机已部署或生产验收通过。

## 4. 集成 / 真机（opt-in，不在默认 CI）

需要真实环境变量与素材，默认跳过：

- 两条真实任务观察（不同脚本/素材），人工或 Mac 审阅。
- `JY_EVAL_MODE=observe` → 收齐观察收据后再考虑 `enforce`。
- 凭据缺失必须有界失败，禁止硬编码兜底。

## 5. 手工验收看什么 artifact

- `report/` 下选镜、场景规则、视觉 QC JSON。
- `last_result.json` / `task_log` 终态与未认证理由。
- 剪映打开草稿：媒体不丢、口播轨无异常气口、字幕在画布内、BGM 独立音轨（若启用）。
- 共享交付后：时间轴字面路径仍指向可读文件。

## 6. 改闸门时的强制项

任何放宽或新增闸门：

1. 增加「必拦样例」测试。
2. 不把 formal 降到 preview 语义。
3. 不在回复/PR 中声称「已上线」。
