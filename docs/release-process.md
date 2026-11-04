# 源码包发布与部署边界

当前维护源码固定在 packages/video-v1.5.3，版本从包内 VERSION 读取。日常功能和文档修改不刷新库存、不创建 tag、不部署。历史包只读；索引实验沿用各自说明。

## 当前包的唯一发布入口

从 packages/video-v1.5.3 执行：

~~~powershell
uv sync --frozen
# 局部交付迭代；输出必须是包外且尚不存在的目录。
uv run --frozen python -X utf8 -m tools.release_package build --output $deliveryOutput --checks delivery
# 发版候选；在全新解压副本运行完整回归。
uv run --frozen python -X utf8 -m tools.release_package build --output $releaseOutput --checks full
~~~

合包工具生成单份文件路径库存、ZIP、测试日志与收据；解压用 ZIP CRC 检查，保留一份 ZIP SHA256 供下载核对。当前包不生成逐文件 SHA 清单或源码树 hash。

~~~powershell
uv run --frozen python -X utf8 -m tools.release_package verify --package $unpackedPackage
~~~

verify 核对版本、必需文件、目录库存与路径边界，不证明真实画面/字幕质量。已有输出目录不覆盖；失败后保留结果用于检查，并选择新输出目录重试。

工具的完整合同见[已验证修复合包说明](../packages/video-v1.5.3/doubao-jianying-orchestrator/references/validated_fix_to_release.md)。

## 候选检查与 GitHub Release

1. 按[测试策略](test.md)通过包级、编排器和架构检查；涉及媒体时记录 FFmpeg 条件、实际执行数和 skip 原因。
2. 将待发布代码提交后，对同一候选执行 full 合包和全新解压验证。源码修改后重新验证，不能沿用旧包测试结果。
3. 核对 VERSION、路径型 release-manifest、包内容与发布说明；只有准备发布或实际文件库存变化需要时才更新相应库存，不为普通修改增加身份门槛。
4. 准备 tag、对应 ZIP、下载核对文件及说明，明确候选、预发布或正式状态；发布按本次授权执行。
5. 同步根 README 的下载入口与 [RELEASES](RELEASES.md)，把可下载附件与当前功能分支区分。

附件、tag 和发布说明必须对应同一候选。合包成功仅证明源码包可重建；生产运行身份以部署机器回读为准。

## 生产安装与实际交付

部署是单独动作：先保留在用配置和任务结果、安排现有批次，再核对实际安装文件及 ACTIVE_VIDEO_RUNTIME.json；源码合并不会自动升级运行机器。日常开发不注册计划任务、不启动真实队列、不读写业务表或 NAS。

安装后可显式核对候选库存中每个文件在两处执行面的实际字节：

~~~powershell
uv run --frozen python -X utf8 -m tools.delivery_runtime --package $candidatePackage --pipeline $pipelineRoot --skills $skillsRoot --all-declared
~~~

该命令只读，退出码0表示声明文件与候选一致，2表示阻断；相同VERSION不能掩盖源码差异。空/坏库存、重复路径、Windows路径别名及逃逸、候选缺文件均拒绝。默认不传`--all-declared`时仍只核对原有交付依赖范围。全量模式不扫描未声明的额外文件、不校验外置后端或私有配置，也不更新ACTIVE；候选库存完整性仍由上述release_package verify验证。已授权的宿主定制应先纳入明确的安装候选，不能通过忽略差异伪造一致。此检查不是成片质量门禁，不生成新逐文件哈希清单。

正式任务交付仍需 tools/acceptance 的四项当前证据全部 PASS：INPUT-IDENTITY、RUNTIME-IDENTITY、DRAFT-PACKAGE、VISUAL-QC。独立 QC 必须绑定实际草稿和素材；preview、超时和旧报告不能转正。共享交付继续检查实际媒体引用及可读性。

真实画面、口播、字幕与草稿可编辑性按实际观察单独记录。Windows 打开、编辑和播放结果不能由源码测试数量、包库存或 ZIP hash 推断。运行入口与恢复方式见 [Windows 流水线](windows-pipeline.md)。

## 历史记录

旧包的 PACKAGE_CONTENTS.sha256 和手工 PowerShell 清单流程仅是历史合同，记录保留在 [RELEASES](RELEASES.md) 和对应只读包内，不用于当前包发布。不得为重整仓库而重写历史归档、第三方 vendor 或已有生产草稿。

