# 研究账本能力在交易系统中的实施

2026-11-16。本文跟踪 [暂停时点的缺口映射](research-ledger-mapping-2026-11-16.md) 的后续实现；原映射保留为历史记录，两个 run-v1 与研究源码不改写。

## 分阶段交付

| 阶段 | 内容 | 当前状态 |
|---|---|---|
| 一 | 指标/信号、Call/Put 价差配对与排序、成本闸门、内存阶段账本、观察缺口判定 | 已实现，阶段二 worker 采样已复用信号与候选计算 |
| 二 | 四张验证表、采样身份/游标、跨标的事务预留、worker tick 采样 | 已实现，只读采样在暂停时仍运行 |
| 三 | 标准意图、开空/平空、逐腿编排、完整 paper 执行与重启恢复 | 已实现，四腿真实链路测试通过 |
| 四 | 状态/账本/心跳/最终报告、冻结 manifest 与哈希链、互斥登记、操作台 | 已实现，生命周期与操作台入口已接入 |

## 阶段一接口与口径

`bybitbot.validation` 顶层导出的指标与价差计算为纯函数，`PositionLedger` 修改自身内存状态；数据库与实际成交事实不由该对象代替。持久化与公开行情采样分别由 `validation.models` / `validation.sampling` 承担。

- `SpreadProtocol` 校验有限数、区间、观察时序与压力滑点。可以直接读取冻结 bull-call 协议；原 bear-put 的逐笔预算与本协议的 cohort 预算不同，显式拒绝把该 JSON 静默套成看涨默认值。`direction="Put"` 表示在显式给定的统一协议下计算 Put，不表示重放原 bear-put 协议。
- `evaluate_signal` 支持 strong_up 与 weak_down。输入为连续、对齐、已完成 K 线，时间戳为起始边界；输出记录收盘边界。历史不足返回结构化原因，损坏历史报错。
- `completed_closes` 将原快照校验器的完整 K 线规范开放为原始行入口；`completed_candles` 保留原来的快照请求截止时刻校验。
- `structure_ok` / `choose_candidate` 默认 Call 买低卖高；Put 买高卖低，Delta 必须为负且绝对值落在指定区间。两腿要求相同标的、到期与 USDT 币种。
- `executable_price` 包含不利 tick、入场点差比例、双边深度、数量步长与价格限制；`spread_economics` 包含手续费、收益风险比、止盈可达性、进出场摩擦、往返损失、压力成本和首腿支出。
- 第二腿复核可传 `paid_long` 使用已支付的含费多头成本；仍需当前可执行平仓盘口，但不再要求多头重新满足入场价格/点差闸门。
- `remaining_risk_budget` 只做预留算术，不提供数据库并发原子性；持久化预留属于阶段二。
- `PositionLedger` 记录逐腿方向、严格更新盘口、限价/截止时刻、现金/费用累计和决策到成交的盘口时距。拒绝的模拟成交不会留下半笔现金或费用。到期/研究结束仍持腿时进入 unresolved，保留敞口与风险，不推测结算。
- 当前内存模拟账本只接受整笔数量成交。实际订单的部分成交、撤单确认和重启恢复属于阶段三，真实的晚到成交事实不得因模拟意图已过期而丢弃。
- `exit_trigger` 按研究结束、持有上限、临近到期、信号失效、止盈、止损的顺序返回原因；缺估值时仍能触发时间/信号退出。

## 阶段二采样与持久化

新增 `validation_study`、`validation_position`、`validation_observation`、`validation_ledger` 四表，迁移为 `c314a70d2b91`。运行身份按 paper 环境、账户与 run_id 隔离，重复开始必须匹配原协议与时间窗口。跨标的风险预留在同一 study 行锁下校验和提交，不能并发透支 cohort 预算。

采样使用独立 slot 游标，在 `_market` / `_account_sync` 后、交易开仓资格判断前运行。过期 slot 不补造历史；恢复时记录实际观察时刻，并以最后成功观察计算缺口。失败周期独立写 error、心跳与游标，不更新成功观察时刻。观察、事件和游标在一个事务内提交，重启/重试不会重复写同一 slot。

K 线从公开 unsigned GET `/v5/market/kline` 读取 BTCUSDT/ETHUSDT 现货 60/D，两个周期共用首次服务端时刻作为截止。完整性仍由公共 K 线校验器负责。合成行情提供独立离线 K 线。采样只产出证据和候选，订单接入由阶段三完成；开始/停止命令与报告入口由阶段四完成。

## 阶段三意图与执行

`ValidationOrchestrator.tick(now_ms, allows_open)` 把研究计划翻译成标准 `TradeIntent`，走 `_submit_draft` → `RiskDecision` → `Reservation` → `Executor.submit` → `Order` → `DurablePaperAdapter` → 执行对账 → `LedgerWriter` 的完整链路。腿序为 `waiting → opening_long → opening_short → open → closing_short → closing_long → closed/aborting`；长多成交后才允许开空，停止时先回补空头再平多头。

- 动作映射：`open_long → OPTION_BUY_OPEN/Buy`、`open_short → OPTION_SELL_OPEN/Sell`、`close_short → OPTION_BUY_CLOSE/Buy`、`close_long → OPTION_SELL_CLOSE/Sell`。开空与平空纳入 `NEW_EXPOSURE_ACTIONS`，沿用开仓窗口、TTL 与撤单口径。
- 预留路由：`OPTION_SELL_OPEN` 占用 `coverage_symbol` 的库存，其余买入与平空占现金，其余卖出占自身库存。
- 协议口径风控：验证意图通过 `RiskContext.validation_limits` 使用 `SpreadProtocol` 的首腿支出上限、点差比例与单标的权利金上限，不复用产品单腿限额；产品单腿限额保持不变。协议不可读时验证意图一律拒绝。
- 协议价格钳制：验证意图以 `protocol_price_limit` 为边界，超出边界直接拒绝追价；物理价与钳制后的 `price_limit` 一致。
- 覆盖空头授权：paper 空头仅在 `covered_paper_account` 按规范事实（空头成交数量、唯一覆盖合约、`short_qty ≤ long_qty`、实际多头与库存可信）逐项对账后才被信任；对账与负债保护共用同一授权口径，授权空头估值计为负债。
- 持久化事实：意图保留 `validation_position_id`、`coverage_symbol` 与编排器 signal 身份；撤单/过期沿 `_cancel_expired` 收口，暂停时新增敞口挂单会被撤销。

## 阶段四生命周期与证据

`ValidationLifecycle` 把 `ValidationStore` 的四张验证表与 `validation/archive.py` 的不可变证据串成运行级生命周期：

- 冻结：启动时一次性钉住协议哈希、当前产品源码逐字节清单与策略版本绑定；后续每个 tick 逐字节复核，源码或绑定变化一律 fail closed。
- 心跳与检查点：每个 tick 更新心跳，并把状态、持仓、观察与错误写进 `ValidationLedger` 的检查点事件；终态检查点最后写，保证归档末尾锚定最终状态。
- 互斥：显式登记对向验证运行的编号、方向/策略口径、协议哈希、状态与账本头；任一登记对象的状态改变即停止验证执行，不靠风险敞口间接推断。
- 终态：`VALIDATION_STOP` 将运行标记为 `ended`；未平仓腿改记为 `study_ended_unresolved` 并保留敞口与风险，绝不伪造结算。
- 归档导出：只导出当前环境/账户的运行，`archive_bytes` 复核哈希链、观察哈希、协议哈希、检查点与冻结源码后生成可复现 ZIP，内含 `STATUS.md`、`FINAL_REPORT.md`、`state.json`、`ledger.jsonl`、`heartbeat.json`、`manifest.json` 与 `frozen/` 源码副本。
- 命令：`CommandKind.VALIDATION_START` / `VALIDATION_STOP` 与其他控制命令一样先入持久化队列，再由持有执行租约的 worker 校验执行；停止命令只改验证运行账本，不发交易所写请求。

## 验证与边界

新增测试覆盖冻结协议解析、信号阈值、坏输入、Call/Put 算术、共同数量步长、深度/预算限制、候选排列稳定性、原 bull-call 纯函数差分、逐腿拒绝原子性及未解决终态。全套项目回归结果记录在对应 PR 中。

阶段二验证：项目 Python 回归 1456 项通过，操作台 Node 测试 6 项通过，Ruff 全部通过。新增迁移在新建的独立 schema 中完成 upgrade/check/downgrade/upgrade/check，未操作已有账本。Alembic 仍输出既有 `path_separator` 配置弃用提示。

阶段三验证：项目 Python 回归 1523 项通过，Ruff 全部通过。四腿真实链路用例驱动真实 `TradingService`、编排器、执行器、paper 适配器、对账与账本，行情取冻结 bull-call 快照 `market-4o2c_dfk` 的 ETH Call 报价（仅到期日平移）；覆盖买腿成交、开空成交、库存方向预留、回补空现金预留、平多库存预留、授权空头估值负债与暂停撤单。未发送任何真实交易请求。

阶段四验证：项目 Python 回归 1547 项通过，操作台 Node 测试 10 项通过，Ruff 全部通过。

当前交付覆盖阶段一、二、三、四。测试使用离线数据、模拟凭据和隔离数据库 schema，未装载实盘凭据或发送交易所写请求。

这些数据可用于模拟定价回测，但不能替代历史期权真实报价。
