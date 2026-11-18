# 按当前素材处理难单

## 利用现有素材做定向判断

`orchestrator.selection.ai_editor` 的初稿、`shot_candidates.json` 与同 SKU 观察库可以提供候选。订单里的原片清单决定允许来源；视频文件相邻只是一种检索线索。对一个有希望的窗口，用实际入点、出点、音轨时长和前后镜头做判断。查到“4片”还要在最终切片和实际显示大小看字。

正常整单沿用全片初选。已有可靠的局部匹配走 `refill_indices`。失败单还没有完整匹配、而 Agent 已在同款素材中找到明确候选时，可以用[定向窗口接口](../orchestrator/selection/directed_display.py)复核真实像素，交回原有选镜器装配。每次只改需要的行，沿用同一次的像素观察和重试预算。普通展示和具体卖点各走自己的复核，跨句实验须由实验组接口复核完整演示。

## 使用已复核的窗口

硬单可从同一次尝试的 `ai_editor` 目录调用 `directed_display.freeze_intake(manifest, analysis, timing, directory)`。它取得整单实际句子分类，绑定最终音频及其内容身份。调用 `review_window(..., index=..., shot_id=..., start_us=..., directory=本句复核目录, inventory_dir=整单目录)` 提议一个原速窗口。成功时将返回行放在对应的选镜行，保留函数记录的像素观察。用 `match_reviewed(manifest, analysis, plan, timing, inventory_dir=整单目录)` 重验完整计划；相邻实验镜头交由 `experiment_groups` 共同复核。

例如在还没有完整初稿的单上，复用本次的实际输入和报告目录。先取整单计划，再把每个成功的单句行放回原位置。续跑时从当前计划恢复，相同候选复用原复核记录。

```python
from orchestrator.selection import directed_display

plan = directed_display.start_plan(manifest, analysis, timing, intake_dir)
row = directed_display.review_window(
    manifest, analysis, plan, timing, index=index, shot_id=shot_id,
    start_us=start_us, directory=review_dir, inventory_dir=intake_dir,
)
plan["rows"][index - 1] = row
```

实验或连续动作由相应模块的 `review_group` 和 `install` 写回同一计划。全片选齐后用 `match_reviewed` 输出正常 `shot_match_report.json`，原始 `input.json` 和 `narration_timing.json` 与其同目录，一起用 `recover --stage draft_write` 续做。

机位使用建议模式时，`camera_variety.accept_suggestion(manifest, analysis, plan, timing, number, inventory_dir=整单目录)` 返回采用候选后的新计划和实际匹配。编号从零开始。它会一并保留连续组成员和接缝复核，原镜头布局已经变化时需另行比较。保留原镜头可直接沿用当前计划。把匹配和真实音轨交给正常写稿恢复入口，并在实际时间线上核对选择。

## 让一段实验看得清

`orchestrator.selection.experiment_groups.observe_pair` 消费当前允许的两条素材及源窗，由像素判断是否为同一次实验，并定位倒液、吸收、纸巾按压、提起与结果。先用完整时间线核对，再决定是否收短长时间静置的尾部；新的观察窗必须重新核实。查看成片中每个相位实际占多少帧。跨源衔接时核对两侧巾面的褶皱、液痕位置、颜色和包装摆位。实验关联有实图证据、自动观察仍把擦拭或移出画面划错时，可用已有 `phase_spec_template` 查看同一批编号原帧，按实际所见提交 `bind_phase_spec`，把真实的动作边界交回 `review_group` 和成片独立复核。保留原图、方案和审阅来源。

试验 manifest 可在**某个实验组首句编号**下指定某个相位要多留的最少输出帧，单位是当前项目帧率。比如第七句开始的一组可用以下输入。具体数字由 Agent 看本单素材和音轨决定。

```json
{"experiment_phase_hold_frames": {"7": {"pour": 12, "result": 18}}}
```

可选相位为 `pour`、`absorb`、`press`、`lift`、`result`。现有配音时长、相位顺序、可见性底线和源窗约束继续参与求解；放不下则给出冲突。新分配用新的组内像素和成片复核。必要时选其他完整实验。技术输入与调用方式见包内[AI 剪辑合同](../../docs/AI_EDITOR.md)。

## 回读实际输出

1. 写稿成功后，从 `tools.native_drafts.documents(draft)["timeline"]` 读取原生稿的活动时间线。
2. 把读取结果另存到草稿外的审片目录。用包内命令 `python -m tools.selection_preview --draft <活动时间线.json> --draft-root <草稿目录> --output <新目录> --name preview` 生成带旁白、字幕和 BGM 的审片 MP4。
3. 结合原生轨道和实际播放，检查句子、字幕、音频和留给动作的时间。运行 `orchestrator/draft_visual_qc.py --draft <草稿目录> --report-dir <本次匹配报告目录> --output <质检报告.json>`。有质量问题时，从相应镜头或关联组继续。记录剪映桌面实播和业务确认的实际状态，FFmpeg 审片件可能与剪映的字体和逐帧渲染有差异。
