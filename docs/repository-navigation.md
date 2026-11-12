# 仓库范围导航

这是开发检索和资料状态的入口，不是第二份运行合同。功能归属沿用 [module_map.json](../packages/video-v1.5.3/doubao-jianying-orchestrator/orchestrator/module_map.json) 与[模块化开发](modular-development.md)；版本读取 [VERSION](../packages/video-v1.5.3/VERSION)，部署身份以实际机器回读为准。

## 从哪一类资料开始

| 范围 | 用途与边界 | 入口 |
|---|---|---|
| 当前维护源码 | `packages/video-v1.5.3/`；目录名是固定历史路径，不随 VERSION 改名 | [Windows 流水线](windows-pipeline.md)、[模块化开发](modular-development.md) |
| 活包内显式实验/诊断 | 在同一活包不等于默认生产；确认每个入口的状态和副作用 | [独立时间线](programmatic-production.md)、[纸品诊断](paper-selection-replay.md)、[句级回放](../packages/video-v1.5.3/docs/sentence-review.md) |
| 隔离索引实验 | `video-v1.5.4/1.5.5` 不是版本升级入口，也不能当无用副本删除 | [1.5.4](../packages/video-v1.5.4/README.md)、[1.5.5](../packages/video-v1.5.5/README.md) |
| 兼容与退役入口 | 旧平铺导入保留兼容；JyPoll/JyRun 只返回退役提示。不得恢复业务实现，也不得仅因文件很薄就删掉 | [迁移合同](modular-development.md)、[AGENTS](../AGENTS.md) |
| 历史包与发布记录 | `video-v1.4.0` 至 `video-v1.5.2` 只读；`releases/` 和旧校验收据保留追溯，不能视作构建垃圾 | [发布边界](release-process.md)、[历史版本](RELEASES.md) |

默认选镜算法 `legacy` / `rule_then_ai` 仍是受支持的显式选择，不等于已退役的模型调用后端。当前调用合同以[模型与视觉流程](model-and-vision.md)为准。

## 默认检索：先找当前实现

以下命令从仓库根执行，仅检索 Git 跟踪文件，不扫描 NAS、生产安装或私有工作目录。把示例关键词替换为实际符号；没有命中时再扩大范围，不能据此判定文件没有外部调用。

```powershell
# 当前源码；排除随包第三方代码，先用 module_map 定位真实功能包。
git grep -n -e 'exact_source_timing' -- packages/video-v1.5.3 ':!packages/video-v1.5.3/jianying-editor/scripts/vendor'
# 当前导航/架构；搜索结果中的日期记录仍需核对有效状态。
git grep -n -e '恢复' -- README.md AGENTS.md CONTRIBUTING.md docs ':!docs/working.md' ':!docs/plans' ':!docs/archive'
# 明确需要历史比较时单独检索，不复制旧实现作为默认方案。
git grep -n -e 'vision_analyzer' -- packages/video-v1.5.2
```

这些是推荐的检索范围，不声称配置能强制所有工具忽略历史。任何删除或迁移还须核对动态加载、CLI、打包和外部 Windows 调用；仓库静态引用为空不是安全删除证明。

## 资料冲突时怎样判断

- 产品目标看 [PRD](prd.md)，架构取舍看 [RFC](rfc.md) 与 [ADR](adr/README.md)，操作看当前子系统说明和真实入口。
- 日期计划、[working](working.md)、[LESSONS](../LESSONS.md)保存当时的意图、失败与验证边界，不替代当前合同。发现冲突时核对实现和后续 ADR，不默默选择看起来较新的文本。
- 源码合入、离线检查、真实样片、剪映验收和生产安装分别记录；文档中的历史通过次数不是当前版本验收。
- 清理只调整信息组织和开发边界，不顺带改变选镜、倍速、QC、交付规则或生产路径。
