# 代码规范（草案）
1. 时间：内部一律整数微秒，入口处 `to_us()`；禁止浮点秒跨模块传递。
2. 状态：只用 RULES.md 列出的 PASS / PASS_WITH_DEGRADED / PASS_WITH_GAPS / FAIL / UNCERTIFIED；异常与超时不能落 PASS。
3. 闸门函数签名 `def xxx_gate(...) -> Optional[dict]`：None=放行，dict=终止并上报原因。新增闸门必须有拦截测试。
4. 配置：凭据与机器路径只读环境变量；缺失即停，禁止降级成硬编码。
5. 版本：从 VERSION 文件回读；版本比较用 `_version_key`，只做下限。
6. 注释：写"为什么/哪次定案"，中文即可；不写过程流水账。
7. vendor/ 不改；需要改时 PR 说明来源与差异。
8. Python 版本、格式化工具（建议 ruff）、类型检查：待确认后补。
