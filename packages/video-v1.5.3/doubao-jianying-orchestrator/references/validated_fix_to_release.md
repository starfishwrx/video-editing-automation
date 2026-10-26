# 已验证修复合入发布包

适用：临时修复经过用户验证，要成为下一次正常生产会使用的功能。

完成标准：维护源码中的正常调用链已接入修复，旧失败有回归证据，包清单和 ZIP 可以重建，全新解压可通过检查；实际部署状态单独记录。仅改临时脚本或只记录经验不算完成。

## 输入与边界

冻结原失败、有效修复、用户确认和适用条件，私有案例放在包外。维护源码、版本包和已安装 runtime 是三个位置；按 `JY_PIPE_ROOT` 与实际 VERSION/manifest 核对，核对运行入口与实际文件，不要求逐层源码哈希。生产期间先做隔离候选，不替换运行文件或恢复旧队列。

通用逻辑应进入包内模块，真实路径改为参数；让正常入口调用必要验证。特殊恢复仍独立显式调用，不能丢失人工编辑或降低质量标准。

本版例子：`tools.draft_delivery.verify_shared` 已接入 `ship_distribute.py`、worker 旧收据续跑以及 enforce 回填审核；`restore-generated` 仍需用户明确确认。完整行为见 [草稿分发与生成版恢复](draft_delivery_and_recovery.md)。

## 可执行的合包验收

从版本包根目录运行，`$releaseOutput` 为包外**尚不存在**的目录：

```powershell
# 局部交付修复：打包、校验、异目录解压，再跑交付和运输相关测试
uv run --frozen python -X utf8 -m tools.release_package build --output $releaseOutput --checks delivery

# 提交发版 PR 前：两套完整测试只在这一步跑一次
uv run --frozen python -X utf8 -m tools.release_package build --output $fullReleaseOutput --checks full

# 只读核对已存在的源码包或解压目录，不更新清单
uv run --frozen python -X utf8 -m tools.release_package verify --package $unpackedPackage
```

命令会用维护中的 `build_manifest.py` 重建清单，检查文件集合、版本和路径边界，生成 ZIP 并解压到新目录，在解压副本里运行所选测试。已有输出目录、缺少文件或测试失败都会停止；失败输出保留作诊断，不自动覆盖重试。

输出目录包含 ZIP、SHA256SUMS、解压副本、测试日志与 `release-receipt.json`。`SOURCE_RELEASE_VERIFIED` 证明源码交付合同，不授予 NAS/飞书写入、远程发布或生产部署权限。生产验收仍要求真实任务、播放和人工审核。

## 回归取舍

先复现旧故障，再测试受影响调用链；只增加能发现独立风险的用例，重复覆盖同一风险时合并。局部迭代选择 `delivery`，发版候选选择 `full`；交付修复复用已有媒体、分析与口播，不重新生成整轮视频。测试数量不是质量指标，必须另有实际故障样本的结果验收。

## 真实故障形成的检查

- ZIP 素材完整、两个副本哈希一致，仍可能保存着失效生产机路径。共享交付必须逐个检查字面引用；相对路径运输包需要受控 import。
- 旧明文 `draft_info.json` 可读，不代表当前加密主时间轴可移植。普通分发应阻断；用户授权恢复原生成版后，使用成功任务证据和完整模板 API 创建新 UUID，保留源稿并比较轨道、素材和时长。
- 修复发生在未提交分支或本机临时目录，不代表发布包已包含。合包时核验模块、技能入口、变更说明和 manifest；PR 对比应保留主分支已有文档，排除其它未提交任务。

仓库发布说明见根目录 `docs/release-process.md`。提交 PR、上传 Release 与部署分别遵循对应授权；本工具均不自动执行这些外部动作。
