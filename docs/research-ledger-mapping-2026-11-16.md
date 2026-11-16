# 研究前向账本 → 交易系统模块映射

交付日期：2026-11-16。本次按要求暂停两个采样器，把连续写入的 run-v1 账本冻结为证据，并逐项核对两个研究的前向账本能力与交易系统（`src/bybitbot`）现有模块的对应关系。**本次没有改动任何代码**，只新增本文与证据提交。

## 1. 暂停时点与证据

采样器在 UTC 2026-11-16T02:21Z 停止（用户要求，早于协议原定的 2026-11-20 观察截止）。两个进程都收到 SIGINT 并正常退出，运行状态、manifest、原始记录与冻结源码副本没有被改写。

| 研究 | 最后一次成功观察 | 采样/快照数 | 结束状态 |
|---|---|---|---|
| `2026-11-15-bull-call-forward`（条件看涨） | 2026-11-16T00:11:18Z | 44 次采样 | BTC/ETH 阶段均为 `waiting`，0 次信号；心跳记录为 `error`（`curl` 退出码 35，取行情失败），之后两轮采集失败 |
| `2026-11-15-options-forward`（看跌价差） | 2026-11-16T02:18:16Z | 70 次快照 | BTC/ETH 价差持仓仍未平仓；观察缺口 2 次（13:17:44Z、02:02:53Z）；心跳 `ok` |

- 冻结的六个协议/源码文件哈希与既有记录一致：bull-call 的 `protocol.json`/`PROTOCOL.md`/`forward.py`/`market.py` 与 bear-put 的 `protocol.json`/`forward.py` 均未变化。
- 六个被持续写入的文件（两个 `run-v1` 的 `STATUS.md`、`heartbeat.json`、`state.json`）已冻结入库，提交 `c097b80`。
- 两个只读观察任务 `bybit-paper-close-summary`、`bybit-bull-call-paper-observer` 已停用，不再读取本地账本。
- 采样链路依赖本机与 NAS 网络：bear-put 通过 SSH 在 NAS 容器内取行情（本机代理 `127.0.0.1:7890`），bull-call 用本机 `curl` 直连。暂停前后都出现过采集失败（SSH 退出码 255、`curl` 退出码 35），失败周期已在账本中留痕，未推测任何触发或成交。
- 恢复方式：用各自冻结引擎的 `--watch` 入口从现有 `state.json` 续跑，协议、引擎与哈希不变。注意 bull-call 的观察窗口被提前截断，缺口与失败周期都已如实留在账本里，不能外推为完整窗口的结论。

## 2. 能力映射表

“研究实现”指两个研究引擎里的位置；“交易系统对应模块”指 `src/bybitbot` 中承担同类职责的位置。状态分三类：**已复用**（公共层落地，两边共用同一实现）、**部分覆盖**（产品里有更完整的对应物，但语义不同）、**未覆盖**（产品侧没有对应能力）。

### 2.1 算术与数据层

| 能力 | 研究实现 | 交易系统对应模块 | 状态 |
|---|---|---|---|
| 期权符号解析与生成 | `bull-call/market.py:303` `_options()` 归一化合约字段 | `terms/symbols.py` `parse_option_symbol()` / `option_symbol()`；`terms/specs.py` `option_spec()` | 已复用 |
| 合约规格与最大数量 | `bull-call/market.py:251` `_instrument()`；`options-forward/forward.py:372` `contract_ok()`（类型、到期、币种、行权价、步长、最小量） | `terms/specs.py` `option_filters()`（宽容）与 `option_spec()`（严格交叉校验） | 已复用 |
| 期权费用算术 | `bull-call/forward.py:164` `fee()`、`options-forward/forward.py:344` `fee()`：`min(taker_rate × index, premium_cap × px) × qty` | `terms/fees.py` `option_fee()`（同式，含可选每张费用）+ `fees.py` `FeeModel`（版本化） | 已复用 |
| 现货费用算术 | 研究不适用 | `terms/fees.py` `spot_fee()` → `fees.py` `FeeModel.spot_fee()` | 已复用 |
| 价格×数量×乘数×汇率估值 | `bull-call/forward.py:410` `value()`、`options-forward/forward.py:408` `value()` | `terms/valuation.py` `option_value()` + `ValuationPolicy` | 已复用算术；取价政策不同（研究按可执行平仓，操作台按 mark 权益） |
| 两腿共同数量步长 | `bull-call/forward.py:307` `common_step()` | `money.py` `common_step()`（LCM 归一） | 已复用 |
| 方向安全取整 | `options-forward/forward.py:431` `limit_price()`（买 ROUND_CEILING / 卖 ROUND_FLOOR） | `money.py` `round_to_step()` / `quantize_price()` / `Rounding` | 已复用 |
| 公开端点白名单与原始响应存档 | `bull-call/market.py:57` `_allowed()`、`:380` `collect()`（每响应独立文件 + SHA-256 清单） | `marketdata/snapshots.py` 端点白名单、`collect()` 落盘 + `manifest.json` SHA-256 | 已复用；产品侧该模块仍是离线工具，未接进 worker 采样循环 |
| 快照完整性校验（路径/时序/分页/K 线） | `bull-call/market.py:92` `_read_snapshot()`、`:118` `verify_snapshot()`、`:123` `_timing()`、`:174` `_catalog()`、`:196` `_candles()` | `marketdata/snapshots.py` `verify_snapshot()`、`completed_candles()`、`normalize_options()` | 已复用 |
| 不利滑点与价差闸门 | `options-forward/forward.py:305` `price()`（买 ask+1tick、卖 bid−1tick、点差比例闸门、tick 对齐）；`bull-call/forward.py:171` `executable()` | `market.py:366` `spread_ratio()`、`risk/engine.py:387` `_check_spread()`（bps 口径） | 部分覆盖：方向安全取整已复用，滑点幅度与价差口径不同 |

### 2.2 策略与决策层

| 能力 | 研究实现 | 交易系统对应模块 | 状态 |
|---|---|---|---|
| 行情 → 信号计算（SMA20/SMA50、24 小时收益、转强条件） | `bull-call/market.py:222` `_signal()`；`options-forward/forward.py:293-301`（小时价 < SMA20 且 < SMA50） | `marketdata/snapshots.py` 只产出完整 K 线 | **未覆盖**：产品侧没有任何指标或信号函数，策略也不使用 |
| 两腿价差策略层（多腿、空腿、行权价配对） | `bull-call/forward.py:215` `structure()`（低行权价多头 + 高行权价空头、到期窗口、宽度/指数带、两腿 Delta 带）；`options-forward/forward.py:372` `contract_ok()`（高行权价多头 + 低行权价空头） | `strategy/templates.py:103` 现货定预算买入、`:157` 单腿期权买入 | **未覆盖**：产品只有单腿买入，没有空腿、没有两腿配对 |
| 候选选择与排序（到期距离、目标 Delta、宽度/指数带、步长取整） | `bull-call/forward.py:318-373` `choose()` | 无 | **未覆盖** |
| 价差级成本闸门（收益风险比 ≥0.25、进出场摩擦、往返损失、2 tick 压力成本、首腿支出上限） | `bull-call/forward.py:251-304` `economics()` | `fees.py:110` `option_fee_ratio()`、`strategy/templates.py` 费率比例 | **未覆盖**：产品只有费率比例，没有价差级经济性闸门 |
| 组合语义与跨标的预留风险（每标的一次入场、不加仓、合计风险预算 25 USDT） | `bull-call/forward.py:313` `remaining_budget()`、`:620-623` 合计不变量 | `risk/reservation.py:89` `budget_view()`、`:142` `reserve_cash()` | **未覆盖**：无 cohort 级预留风险记账 |
| 逐腿时序引擎（意图 → 待成交 → 只用更新的独立盘口成交 → 发出下一腿意图） | `bull-call/forward.py:487`、`:433` `fill()`；`options-forward/forward.py:551-553`（`sourceMs ≤ decisionSourceMs` 即跳过）、`:437` `fill()` | `execution/states.py` 订单状态机、`market.py` `QuoteCache` 新鲜度校验 | 部分覆盖：产品要求新快照才撮合，但没有多腿编排与“决策盘口秒差”留痕 |
| 阶段状态机（waiting→open→closed） | `bull-call/forward.py:91` `new_state()` 的 `phase`：`waiting`/`opening_long`/`opening_short`/`open`/`closing_short`/`closing_long`/`closed`/`aborting`/`aborted`/`cancelled`/`expired_unresolved`/`study_ended_unresolved`；`options-forward/forward.py:93` 同构 | `db/models.py:520` `inventory`（数量/均价/持仓段）+ `service.py:1777` `_exit_rules()` | **未覆盖**：产品按持仓段与退出规则推进，没有 per-base 的 `phase` 枚举，也没有跨“订单级/运行级”之间的中间层 |
| 退出规则族 | 研究：72 小时持有上限、到期前 24 小时、转强信号失效、止盈/止损各按入场借记的 50% | `strategy/templates.py:71` `exit_rules()`（持有期、到期前、利润阈值、权利金止损） | 部分覆盖：规则种类接近，触发口径（借记比例 vs 阈值）与优先级不同 |

### 2.3 账本、证据与运行

| 能力 | 研究实现 | 交易系统对应模块 | 状态 |
|---|---|---|---|
| 每标的现金与费用累计 | `state["positions"][base]["cash"/"fees"]`（`bull-call/forward.py:91`、`:251` `economics()`） | `ledger.py:85` `record_execution()`、`:134` `apply_fill_to_inventory()`、`:263` `record_cash_flow()` | 部分覆盖：产品是成交级账本与可信库存，缺研究式的按标的现金/费用累计列 |
| 账本文件与状态文件 | `report()`（`bull-call/forward.py:664`、`options-forward/forward.py:725`）写 `STATUS.md`；`state.json` 由 `atomic_json()` 写出 | `db/models.py:832` `runtime_state` + `web/app.py:609` `_status()` + `:897` `_audit_csv()` | 部分覆盖：产品是数据库 + 页面/CSV，没有文件型状态报告 |
| 最终报告文件 | bull-call `FINAL_REPORT.md`（`forward.py:833`，仅 `final=True`）；bear-put 无对应文件 | `sandbox.py:360` 写 `verification.json`（`sandbox.py:231` 定义路径） | **未覆盖**：产品只有沙盒验收报告，没有研究型最终报告 |
| 运行期心跳与健康度 | `heartbeat.json` 三态（`ok`/`error`/`ended`）：`bull-call/forward.py:835`、`:865-878`；`options-forward/forward.py:889-924` | `service.py:2084` `_status()`、`execution/lease.py:77` `ExecutorLease.heartbeat()`、`web/app.py:1083` `/health` | 部分覆盖：产品心跳进数据库与页面，无独立心跳文件与“已结束”终态 |
| 协议与源码冻结（哈希钉住） | `bull-call/forward.py:63` `prepare_run()`（源码副本进 `run/frozen/`）、`:639` `verify_ledger()`（每周期复核当前源码与归档副本） | `service.py:86` `config_fingerprint()`（配置身份）、`db/models.py:216` 策略版本化 | 部分覆盖：产品钉配置与策略版本，没有运行级 manifest 冻结 |
| 整档状态哈希链 | `bull-call/forward.py:51` `state_hash()`、`:639-661` `verify_ledger()`（`checkpointHash` + 逐条重算快照哈希） | 无；产品依赖数据库唯一约束去重 | **未覆盖** |
| 采样幂等与重放去重 | `state["processed"]`（快照哈希 → 路径） | `ledger.py:51` `append_event()` 幂等、`db/models.py` `uq_execution_dedupe` | 部分覆盖：产品按成交/计划去重，无采样级去重 |
| 观察缺口记账 | `options-forward/forward.py:694-701` `observation_gap`（>30 分钟）、`bull-call/forward.py:607-617` | 仅 WebSocket 深度缺口（`adapters/bybit_ws.py`） | **未覆盖** |
| 未解决敞口终态 | `expired_unresolved` / `study_ended_unresolved`（到期或研究结束时仍持腿） | `ledger.py:295` `record_option_settlement()` 要求证据、不伪造结算 | **未覆盖**：产品不伪造结算，但没有“未解决”终态 |
| 单实例运行锁 | `runner.lock` + `fcntl.flock` | `execution/lease.py:77` `ExecutorLease`（PostgreSQL advisory lock）、`ledger.py:188` 事务级 advisory lock | 已覆盖：数据库锁强于文件锁 |
| 对向实验互斥 | `bull-call/forward.py:122` `opposite_evidence()`：读对向账本并核对协议/引擎哈希，读取失败即全部阻断（fail closed），读到的证据存进 `run-v1/evidence/opposite-*/ledger.json` | `risk/engine.py:133` `RiskEngine`（`:446` 敞口上限、`:465` 权利金预算、`:309` 账户暂停）、`risk/delta.py:121` `evaluate_delta_bounds()` | **未覆盖**：产品没有“研究/策略互斥”的显式登记，只由敞口与 Delta 区间间接约束 |

### 2.4 产品独有、研究侧没有

订单状态机与幂等成交（`execution/states.py`、`ledger.py`）、可信库存与原子预留（`risk/reservation.py`）、对账（`reconciliation.py`）、告警投递与暂停分级（`ops/alerts.py`、`ops/pauses.py`）、运行状态机（`ops/state.py`）、实盘启用闸门（`settings/models.py` `live_gates`）、沙盒验收报告（`sandbox.py`）。

## 3. 已复用与未覆盖小结

**已复用（公共层已落地）**：合约解释、严格规格校验、费用算术、估值算术、数量步长、方向安全取整、公开行情采集与原始响应哈希校验。研究与操作台现在共用同一份实现，见 `docs/shared-domain.md`。

**未覆盖（产品侧缺口，按影响排序）**：

1. **信号/指标层缺失**——产品只取到完整 K 线，没有任何 SMA、收益率或信号函数；两个研究的“转强”“价差”判定目前无法在产品里表达。
2. **两腿价差策略缺失**——产品只有现货定预算买入与单腿期权买入，没有空腿、没有行权价配对、没有多腿编排；`structure()`/`choose()`/`contract_ok()` 的能力在产品里没有对应物。
3. **价差级成本闸门与 cohort 预留风险缺失**——只有费率比例，没有收益风险比、进出场摩擦、往返损失、压力成本与跨标的预留风险预算。
4. **周期采样任务模型不匹配**——`scheduler.py:65` `Scheduler` 是日历触发的一次性下单计划，去重键 `env:strategy_id:rule_id:planned_at` 与“错过即跳过”的默认语义都不适合周期采样器（采样器需要补齐或记缺口）。
5. **per-base 阶段状态机与现金/费用累计缺失**——产品只有持仓段与成交级账本。
6. **文件型账本/状态/心跳与最终报告缺失**——产品是数据库与页面，缺研究级的可归档报告与“已结束”终态。
7. **运行级协议与源码冻结、整档哈希链缺失**——产品只钉配置与策略版本。
8. **显式互斥登记缺失**——目前只靠敞口上限与 Delta 区间间接约束。
9. **决策盘口到成交的时距留痕、观察缺口记账缺失**——产品要求“必须有更新快照才能撮合”，但不记录研究级秒差，也不统计观察缺口。

## 4. 若要在产品内补齐时的扩展点（本次未实施）

- `scheduler.py`：新增与交易计划分离的去重命名空间（例如 `env:ledger:{instrument}`）与“不跳过、记缺口”的错过语义。
- `service.py:516` `tick()`：在 `_market()`/`_account_sync()` 之后挂一个采样步骤；采样游标放 `service.py:102` `StateStore`（`runtime_state` 的独立 key）。
- 指标与信号：新增纯函数模块（K 线 → SMA/收益率/信号），输入用 `marketdata.snapshots.py` 的完整 K 线，输出结构化信号对象，供策略与验证任务共用。
- 策略层：新增两腿价差策略模板（行权价配对、空腿、多腿编排、逐腿待成交与“只用更新的盘口成交”），并把退出规则按研究口径补齐（借记比例型止盈止损、信号失效）。
- 数据库：新增 `validation_study` / `validation_position` / `validation_ledger` / `validation_observation` 表，参照 `db/models.py:607` `ledger_event`、`:660` `equity_snapshot`、`:832` `runtime_state` 的写法，并补一个 Alembic 迁移（当前 head `8a4d17c2f905`）。
- 运维：`ops/control.py:21` `CommandKind` 增加“开始/停止一次研究运行、生成最终报告”，告警走 `ops/alerts.py:71`，采样停滞用 `ops/pauses.py` 的既有暂停理由表达。
- 页面：`web/app.py:609` `_status()` 增加账本分区，导出复用 `:897` `_audit_csv()`。
- 原子写与目录锁：`src/bybitbot` 目前没有原子写工具与目录锁原语（研究侧在 `forward.py:43` `atomic_json()` 与 `runner.lock` 里自带），若走文件存储需要补齐。

## 5. 边界

- 本次只暂停、冻结证据并写映射文档；**没有改动任何产品代码、协议、引擎或原始记录**，没有装载实盘凭据，没有发送交易所写请求，没有启用生产策略，NAS 部署未变更。
- 两个研究的观察窗口被提前截断，采样缺口与失败周期已如实留在账本中，不能据此判断策略是否盈利。
- 上述映射是按当前代码结构做的核对，不构成实盘能力验收；既有 AC 验收矩阵见 `docs/acceptance.md`。

这些数据可用于模拟定价回测，但不能替代历史期权真实报价。
