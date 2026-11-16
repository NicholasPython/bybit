# 公共领域层与研究迁移

交付日期：2026-11-16。操作台与研究共享费用、合约解释、精度和估值算术。产品新增 `bybitbot.validation` 领域层，提供信号、价差决策与内存账本；原研究 v1 保持冻结。接入进度见 [验证能力实施](validation-delivery-2026-11-16.md)。

## 公共模块

| 模块 | 唯一职责 | 使用方 |
|---|---|---|
| `bybitbot.terms.symbols` | 解析/生成期权符号，统一 UTC 08:00 到期日、执行价、Call/Put 与结算后缀 | REST 适配器、合成行情、严格规格解析 |
| `bybitbot.terms.specs` | 宽容过滤器提取与严格 `OptionSpec`；交叉校验符号、币种、类型、到期、显式执行价 | 操作台合约目录、研究快照 |
| `bybitbot.terms.fees` | 现货费、期权费与看涨交割费算术 | 版本化 `FeeModel`、比较研究、bull-call v2 |
| `bybitbot.terms.valuation` | `ValuationPolicy` 身份与价格×绝对数量×乘数×汇率 | 操作台权益、研究可执行平仓估值 |
| `bybitbot.money` | 价格/数量精度与两腿共同数量步长 | 下单、研究价差数量计算 |
| `bybitbot.marketdata` | 公开端点白名单、响应存档、原始字节哈希/路径/时间/分页校验、完整 K 线、严格期权归一化 | bull-call v2 的 `market.py` |

外部依赖不能进入纯 `terms` 层。订单执行、资金预留、对账、策略信号和 UI 都不属于这个包。`delivery_fee` 当前计算看涨内在价值；不能直接用于看跌交割。

## 费用、规格与估值的边界

期权限价最大数量优先取 `maxLimitOrderQty`，不存在时回退到 `maxOrderQty`。操作台允许缺失规格进入目录展示，实际授权时拒绝不完整合约；研究入口要求完整且一致的规格，不悄悄修复元数据。

期权费为 `qty × (min(index × underlying_rate, premium × premium_cap_rate) + per_contract)`；无效或非正指数保守使用权利金费用上限。费用版本仍由 `FeeModel` 配置及模拟检查点保存。

操作台 `equity-v1` 使用 mark 计算账户风险权益；研究 `bull-call-executable-liquidation-v2` 按持腿方向、不利盘口和假设平仓费计算净损益。双方共享算术与政策类型，各自保留取价与汇率政策。缺价、非法值、负乘数等产生未知/不完整结果，不能当作零价值。

## 研究接入与证据保护

比较研究 `2026-11-15-call-spread-comparison/analyze_public.py` 已改为调用公共规格/费用。独立 `verify_results.py` 继续保留自己的公式作为校验基准。保存的公开快照重放后，`analysis.json`、`all_candidates.json`、`candidates.csv`、`verification.json` 与原结果逐字节相同。

原 bull-call v1 与 bear-put v1 仍按原冻结引擎运行。六份协议/源码在此次交付时均与既有哈希一致；它们的运行状态、manifest、原始记录与采样进程未被实现代码改写。

新引擎位于 `research/2026-11-15-bull-call-forward-v2/`，尚未启动采样。它保留 v1 的信号和逐腿状态机，使用独立 `run-v2` 和协议。运行前递归冻结全部本地导入源码及包初始化文件，每次处理重新校验源码和归档；原实验目录及其祖先、子目录和符号链接别名会在写入前拒绝。两腿分别等待下一份独立盘口，不能在决策使用的旧报价上成交。

验证包括 40 项快照/v2 测试、原引擎回归，以及 10 份保存的公开快照 v1/v2 完整归一化对照。详见 v2 README 和 PROTOCOL.md。

这些数据可用于模拟定价回测，但不能替代历史期权真实报价。
