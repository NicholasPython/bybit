# 双腿价差套利阶段三：滚动价差信号与受控触发

日期：2026-11-22。阶段一、二说明见 [阶段一交付](spread-arbitrage-phase1-2026-11-20.md) 和 [阶段二交付](spread-arbitrage-phase2-2026-11-22.md)。

本阶段把滚动对数价差、Z-Score 阈值、预期成本过滤和硬止损接入阶段二的持久化 paper 执行器。默认关闭；没有部署到 NAS，没有使用交易凭据，也没有开放真实发单入口。Walk-Forward、参数寻优、影子盘、灰度晋升、私有流端到端和实盘晋级均不在本阶段交付。

## 信号口径

`spread/signals.py` 是纯计算层，不访问数据库或交易所；输入是时间严格递增的双腿样本，输出带理由码的结构化决策：

- 对数价差 `ln(price_a) - β·ln(price_b)`，使用总体标准差计算窗口均值与 Z-Score，全部以 `Decimal` 复算。
- 样本时间重复、乱序、缺口超龄、价格非正、窗口不足或标准差退化时返回不可用，不产生交易动作。
- 预期成本 `2·notional·fee_rate + 2·notional·slippage_bps/10000 + funding_buffer` 先于阈值判定：`|spread-mean|·notional` 不足以覆盖成本时直接过滤。
- `|z| ≥ z_stop` 的硬止损优先于开仓；`|z| ≥ z_entry` 开仓，`|z| ≤ z_exit` 平仓，中间地带不动作。
- 开仓方向：A 相对高估为 `SHORT_A_LONG_B`，A 相对低估为 `LONG_A_SHORT_B`。
- 动作与理由码都是稳定英文字符串，便于 worker 分派和审计留痕。

窗口长度至少为 5：三者以内的窗口使最新点的 Z-Score 上界为 √2，无法触发常见止损阈值，因此配置与纯计算层均不接受过小窗口。

## 持久化窗口与冷却

新增迁移 `b7c9e1f4a2d8`，接在阶段二 `a612e47bc903` 之后：

- `spread_signal_sample` 按 `(environment, pair_key, at_ms)` 唯一约束保存双腿中间价与原始盘口；按采样间隔分桶，重启后继续累积窗口，不读取或覆盖公共 `quote_snapshot`。
- 腿价格必须是有限正数、对齐 tick size，合约必须可交易且盘口状态可用；无效行情不会污染窗口。
- 最近一次决策写入 `runtime_state`，键为 `spread-signal-decision:{pair_key}`；硬止损写入 `spread-signal-stop:{pair_key}` 的冷却截止时间。两者都用事务性 upsert，重启不丢失。

`spread/auto.py` 负责采样、取窗口、生成 `PairSignal` 或退出请求。它不自行下单，也不绕过阶段二权限入口。开仓配对编号由时间戳、进程内序号组成，阶段二仍做完整载荷幂等校验。

## worker 接入与权限边界

`TradingService.tick()` 只在以下条件同时成立时评估自动信号：

1. 执行租约有效、对账校准通过、无活动暂停；
2. `spread.auto.enabled` 与对应配对开关均启用；
3. 合约规格、双侧报价新鲜度和时间窗口均有效。

开仓调用 `await service.open_spread(signal)`，平仓调用 `await service.close_spread(pair_trade_id, reason="auto:...")`。两者继续要求账户身份、近期快照、对账、完整估值、风险基线、已确认租约与无暂停；被拒绝时只记录告警，不绕过门槛。有效性异常只跳过当前币对，不污染主循环。

配置示例新增默认关闭的 `spread.auto` 段：窗口、采样间隔、费率、滑点、资金费缓冲、最大信号年龄、止损冷却与目标名义。采样间隔不得短于 worker 轮询间隔；启用套利仍仅限独立 USDT unified paper 账户，不能与旧策略混跑。

## 验证

使用合成账户、行情与订单；数据库用例使用隔离 PostgreSQL schema，未访问交易所私有接口。

```bash
uv run --frozen pytest -q tests/test_spread_auto.py tests/test_spread_signals.py tests/test_spread_durable.py
```

阶段三新增 45 项测试：41 项纯信号计算与边界，4 项持久化窗口、冷却重启、无效行情隔离和 worker 权限集成。迁移 head 为 `b7c9e1f4a2d8`。

## 尚未覆盖

- Walk-Forward、Optuna/参数寻优、影子盘、灰度放量、实盘晋级。
- 真实资金费率、 Funding 收支预测、交易所手续费阶梯与真实盘口延迟。
- 私有 WebSocket 到自动信号的端到端推送；当前使用 worker 轮询与公共盘口快照。
- 配对级告警订阅、机器人通知以及自动止损后的运营复核流程。
- 长期窗口容量、跨日样本清理与统计漂移监控。

默认配置不启用自动信号；阶段三不构成真实资金验收。
