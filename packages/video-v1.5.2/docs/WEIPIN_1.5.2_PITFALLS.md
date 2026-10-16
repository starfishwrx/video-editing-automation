# 卫品 1.5.2 踩坑与优化沉淀（bench10 / r26–r30）

面向下次卫品批跑与初步交付。技术细节以 `doubao-jianying-orchestrator/CHANGELOG.md` 与规则包版本为准；本文只记「别再踩」的操作与判定口径。

规则包版本（本稿对应）：`weipin.json` → **1.5.2-9**（2026-10-15 飞书反馈修正，未部署）；上一次运行时部署：**1.5.2-r30**（规则包 1.5.2-8）。

---

## 1. 交付与回写

| 坑 | 正确做法 |
|---|---|
| 本地 `local_only` 出完稿就当交付 | 初步交付必须：人工检查凭证 → NAS 分发（不覆盖）→ 回写工作台「草稿文件」+「问题反馈」 |
| 预览稿回写「已完成」 | **禁止**。QC 未正式过门前任务状态保持「待完成」，反馈写明「初步交付/预览稿」 |
| 用错飞书表 | 卫品生成工作台是 `tbl2glS3nYwM5i5m`，不是纸品/历史总表 `tblUSuZw9hw97gnn` |
| NAS 目标已存在仍 robocopy | **停止**，换新 `-rev-N` 或人工确认后另路径；不得覆盖已有剪映编辑稿 |
| 只改分析规则就全量重看素材 | 用 `recheck` 增量重看（见下），禁止再开一轮 r25 式全量 vision |

本机收尾脚本参考：`D:\jy_pipeline\ship_weipin_bench10_preview_delivery.py`；凭证样例：`work/delivery_check_weipin_bench10_*.json`。

成片 mp4 导出：流水线只出草稿，不出 mp4；本机剪映 11.5 的界面不对 UI 自动化暴露控件，`jianying_controller.export_draft`（只支持剪映 6 及以下）不可用，不要再去试。用 ffmpeg 按明文 `draft_info.json` 渲染：

```
cd D:\jy_pipeline\work\preview_exports_20261012
python -X utf8 export_batch.py <plan.json> <输出子目录>
```

`plan.json` 格式 `[{"record_id": "...", "drafts": ["草稿名"]}]`；自带抽帧质检（时长、黑帧、字幕、音量）。抽到黑帧时先查是否落在 `visual_missing_placeholder`（预览稿本身留黑），不是渲染错误。草稿被剪映打开过后 `draft_content.json` 会加密，脚本只读 `draft_info.json`，不受影响。

---

## 2. 画面语义（最易误配）

| 坑 | 正确做法 |
|---|---|
| 「吸水层」/对光透光镜头当成倒液吸水实验 | 透光 = **轻薄/透气**；吸水实验只认倒液证据。规则：`shot_exclude_keywords` + 吸水画面词收紧（1.5.2-7） |
| 短口播装不下倒水→直接留黑 | 优先真实验：倒水可 burst 到 `pour_burst_max=4.0`（1.5.2-8），再不行才黑 |
| 指产品动作 5–20 秒塞进 1–2 秒口播→留黑 | `point_front`：settled → lead_in → pool → 黑（1.5.2-4） |
| 「量大」配成堆产品盒 | 卫品匹配按卖点：量大 → 吸收容量/锁水实验画面（1.5.2-5） |
| 同文件名多目录副本换镜后整稿 `DUPLICATE_VISUAL_SHOT` | 场景补镜按**文件名**去重，并遵守本机 `safe_source_limit`（常见 23） |
| 「独立包装」「干净卫生」「随身带」配成密封袋面特写 | 拆包 `tear_pack` 文案命中即强制要撕开/取出动作画面，换镜优先「拆包→展开面层」连续镜头（1.5.2-9）；单句放不下完整拆包时跨相邻句并组连续播（1.5.2-13） |
| 连着两句都是外包装静态（开场句不含包装词，A–D 关键词不触发） | 上一句袋面静态、这一句文案讲包装或选中的又是袋面 → 强制拆包（`trigger: prior_static_pack`，1.5.2-9）；品牌/促销/报数量句不跟 |
| 「卫生巾」触发「卫生」卖点 | `text_ignore` 去掉卫生巾/卫生用品/卫生棉后再匹配 |
| 描述「未拆封」被画面词「拆封」当成拆包 | `shot_ignore` 先去掉未拆封/未开封再匹配 |

---

## 3. 缓存与重跑

| 坑 | 正确做法 |
|---|---|
| 改分析文案后整池重分析 | 分析要求写成 `{"text","recheck"}`：不进缓存键；只重看命中 `recheck` 词的旧描述 |
| 改选镜/场景规则却以为要重分析 | 选镜与场景规则读已有 `shot_candidates`；无必要勿删 `.cache` |
| 容量档案磁盘剩余过期导致 `MATERIAL_BATCH_LIMIT` | 刷新 host capacity 档案的磁盘统计后再建稿；上限以校准值为准 |

---

## 4. 变速与装配

| 坑 | 正确做法 |
|---|---|
| 定速点吸附后误报 `SPEED_OUT_OF_RANGE` | 不变量校验容差按 1µs 栅格，勿用过严的 1e-6 卡死 |
| 预合成时长与引擎差一帧 → 证据卡满后退回 0 秒片头 | TTS/预合成与引擎共用同一探测口 |
| 连续吸水句每句各演一遍实验 | 同源三段跨句分组（含 bridge / borrow），整组演一遍 |
| 配音轨句间有空隙（运营反馈「配音没连上」） | 卫品 `voice.max_gap_frames=1` → `tail_pad_s=0`，配音首尾相接；倍速仍 1.2（1.5.2-9） |
| 运营说字幕「Y=-1000」就写 `transform_y=-1000` | **禁止**：那是像素口径，会把字幕推出画布。半画布口径默认 `Y=-0.55`（由 -0.8 上移） |

---

## 5. 本机与并发

| 坑 | 正确做法 |
|---|---|
| 弹出控制台窗口 | 子进程 `CREATE_NO_WINDOW`；批跑勿用会闪窗的启动器 |
| NAS DPAPI 密钥缺失就开历史队列 | `ACTIVE_VIDEO_RUNTIME.json`：`queue_started=false`；缺密钥勿自动启 JyPoll |
| 草稿名冲突覆盖 | 一律新 `-rev-N` 原子预留；本地已存在则跳过/递增 |

---

## 6. QC 解读（初步交付时）

- `PASS` / `PASS_WITH_DEGRADED` / `PASS_WITH_GAPS`：可给人审，仍非自动「已完成」。
- `FAIL`：常见是包装/静帧/擦拭缺失等后验；**功能镜头对了也可能 FAIL**——交付反馈必须带真实 QC，勿粉饰。
- `UNCERTIFIED`：相位或场景未核实；给人审时标明「未认证」。

bench10（2026-10-15）初步交付：10/10 NAS + 表格草稿链接；仅天山绒棉为 `PASS_WITH_DEGRADED`，其余 FAIL/UNCERTIFIED，状态均保持「待完成」。

---

## 7. 建议回归清单（下次批跑前）

1. 吸水句组：S 句是否为倒液/吸收，而非透光翻面。  
2. 短吸水口播：是否出现真倒水而非黑屏。  
3. 指产品短句：settled/lead_in，而非黑。  
4. 素材数 ≤ `safe_source_limit`，无 `DUPLICATE_VISUAL_SHOT`。  
5. 改规则后：确认走了 recheck 而非全量 vision。  
6. 交付：凭证 → NAS 新目录 → 正确 table_id → 不写「已完成」。
7. 拆包句（独立包装/干净卫生/随身带…）：画面是撕开/取出动作，不是袋面；`scene_rules_report.json` 无 `SCENE_NO_FOOTAGE` 遗留。
8. 配音轨无空隙（`tail_pad_s=0`）；字幕在画面内、位置 `Y=-0.55`。
