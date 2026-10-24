# 代码规范（v1.5 系列）

1. 时间单位
   - 内部一律“整数微秒”口径。入口/出口用 `timing_contract.to_us()/from_us`；禁止在模块间传递浮点秒。
2. 状态与闸门
   - 引擎闸门函数形如 `def xxx_gate(...) -> Optional[dict]`：`None=放行`，`dict=终止并上报原因`。新增闸门必须有“必拦样例”的测试。
   - 视觉/结构/时序等证据不足不得判 `PASS`；仅允许 `UNCERTIFIED` 或阻断类状态（详见 orchestrator-engine 文档的状态枚举）。
3. 配置与凭据
   - 运行依赖、NAS/表格/云端凭据取自各模块支持的进程环境变量；完整使用说明见包内 `ENVIRONMENT.md`。`environment_report()` 不覆盖云端凭据认证，缺失凭据不得使用硬编码兜底。
4. 版本与兼容
   - 任何运行时版本取自 `packages/video-v1.5.3/**/VERSION`；比较一律走 `_version_key` 做“下限”判断，不写死等值。
5. 注释与中文
   - 代码注释只写“为什么/口径/权衡”，中文即可；不写流水账。与用户定案/事故复盘相关的口径请在注释中留下检索点。
   - 当前默认型号写全为 GLM-5.3-Flash / `glm-5.3-flash`；GLM-5.3 与 ChatGLM3 是其他型号。默认 transport、选镜模式或 QC 合同变更时，同步模型、架构、配置和入口文档；旧行为标注 legacy/历史适用范围。
6. 目录与第三方
   - `vendor/pyJianYingDraft` 属第三方；不改。如需改动，PR 必须说明来源、差异与风险。
7. Python 版本与依赖
   - 依赖只用 **uv**（`uv sync` / `uv add` / `uv run --frozen`），不要对本包 `pip install`。
   - 运行环境 Python >=3.11,<3.13；默认 CI 使用 3.11。
   - 声明以活包根 `pyproject.toml` / `uv.lock` 为准；历史 README 里的零散包名列表仅供参考。
8. 媒体/时序一致性
   - FFmpeg 解码优先 GPU（`video_decode.run_ffmpeg_decode` 自动注入 `-hwaccel cuda -threads 1`）；4K HEVC 必须带 `-threads 1`，否则 NVDEC 会静默回退 CPU。
   - 编码优先 `h264_nvenc`，不可用时按 `video_encode.encoder_plan` 级联回退（含 `libx264`）。
   - 旁白响度统一 -16 LUFS，采用“纯增益 + 真峰值限幅”，并固定输出时长（见 `audio_loudness.normalize_narration`）。

