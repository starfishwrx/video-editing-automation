# 草稿分发与生成版恢复（1.5.3）

## 交付成功的含义

同事在可访问同一 NAS 的电脑上复制或解压草稿后，工程里的每一个媒体路径都能直接读取素材。仅有文件数、ZIP 完整或复制哈希一致，不能证明这一点。

`ship_distribute.py --style unc` 仍是正式 finalizer 的分发入口。它在原有源/目标哈希检查后，额外核验主目录和分发目录中所有可读时间轴的**实际绝对路径、素材字节和草稿注册信息**；失败返回非零，不产生成功分发回执。不要绕过原有 Eval、人工审核或飞书回填门禁。

两种包必须区分：

- **共享路径交付稿**：引用 `JY_NAS_SHARE` 下固定目录的绝对 UNC 路径。接收方连接同一 NAS 后可直接使用，无需 Python；移动或删除 NAS 原目录会使这些引用失效。
- **相对路径运输包**：`nas_portable export` 的 `media/...` 加哈希清单，用于受控 import。本身不承诺剪映直接识别相对路径。需要离线本地稿时先用官方 `nas_portable import` 导入到最终位置。

目录结构沿用调用方批准的交付规则。修复入口要求每份草稿独立命名；两条生成结果可分别命名为 `<名称>`、`<名称>-2`，无需新增“版本1/版本2”父目录。

## 诊断与核验

从包根目录运行；示例参数均为调用方提供的真实路径。

```powershell
uv run --frozen python -m tools.nas_portable inspect --help
uv run --frozen python -m tools.draft_delivery verify-shared --draft $receivedDraft --reference-root $nasMainDraft
```

`verify-shared` 读取根目录和 `Timelines` 下的实际时间轴，拒绝加密文件。校验引用必须直接命中 `$nasMainDraft/media`，不能靠“文件名相同，在包内找到了另一个文件”的兜底通过。分发副本或接收方副本的注册路径仍应指向主 NAS 目录。

## 已有可读草稿重新交付

```powershell
uv run --frozen python -m tools.draft_delivery publish-shared --source $sourceDraft --share $nasShare --name $newName --work $newEvidenceDirectory
```

使用新的名称和证据目录，NAS 会话需已登录。入口通过官方 transport 收集媒体、导入独立 staging、转换共享路径，再发布主目录与分发副本。源稿和已有目的目录不会被覆盖。相对运输包的证书只在新 staging 中移除，避免拿旧证书认证 UNC 衍生稿。

产物包括 ZIP、异目录解压副本、`delivery-receipt.json`；要求复制哈希一致、所有文字路径可达、素材字节相同、解压后仍可读取 NAS。中途失败的目录保留用于诊断，重试用新目录。此入口是修复工具，不能用来跳过正式交付授权或把 preview 改成 formal。

## 加密稿：必须区分当前编辑版与原始生成版

剪映打开草稿后，实际 `draft_content.json`、`Timelines/<主时间轴>/draft_content.json` 或元数据可能加密，而旁边 `draft_info.json` 仍是旧明文。改旧明文不能修复当前加密时间轴。普通分发仍须阻断；工具不解密、不迁移加密编辑会话。

只有用户明确确认**没有后续编辑需要保留，并同意恢复该原始生成快照**，才能执行：

```powershell
uv run --frozen python -m tools.draft_delivery restore-generated --source $encryptedDraft --task $originalTaskJson --result $originalResultJson --destination $newLocalDraft --confirm-generated-version
```

任务和成功结果必须与快照名称、时长一致。工具以 bundled `JyProject` / `ScriptFile` 模板 API 创建新 UUID，要求全部轨道、素材和时长保持一致，并保留源目录。返回的 JSON 应由调用方保存到外部证据目录；随后用 `publish-shared` 分发恢复稿。

成功记录的名称、时长不是对任意明文文件真实性的密码学证明；调用方必须先确认它确实是对应原始生成快照。快照、任务、结果与源树哈希进入回执。存在人工编辑、证据不一致或快照损坏时停止恢复。

不要逐轨重导历史模板：可能裁掉未直接引用的字幕/效果素材，或因历史悬空引用失败。完整模板容器导入后要比较所有轨道和素材。

## 验收边界

`SHARED_MEDIA_VERIFIED` / `GENERATED_VERSION_RESTORED` 只证明各自文件级合同；`desktop_playback_verified` 和 `visual_verified` 保持 false。需要另做接收方剪映打开与播放验证。恢复稿继承原任务的 preview/formal 事实，不能新增质量 PASS。

回归命令：

```powershell
uv run --frozen python -m unittest discover -s tests -p test_nas_portable.py -v
uv run --frozen python -m unittest discover -s tests -p test_draft_delivery.py -v
```
