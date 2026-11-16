# 交易与盈利准备度代码复核

核查日期：2026-11-01（Asia/Shanghai）。本报告依据本机 `/Users/nicholaswang/Desktop/code/bybit` 源码、项目文档及纯内存复现。没有读取 secrets 内容，没有连接交易账户、数据库或发送真实交易请求，没有修改应用代码。**本次子任务未独立核验线上容器与本地源码是否同版本，因此以下代码发现不能直接当成线上已触发的故障。**

结论：当前证据支持 paper 模拟/管理功能的开发验证，不支持“已能用于实盘盈利”。存在两个需要优先修正的业务问题，以及真实交割等上线阻塞项。

## CR-01 · P1 · 历史已平仓批次的持有期规则会立即卖出新买入库存

**实际执行的纯内存复现：** 同一策略、同一 BTCUSDT 合约，第一批在两天前买入并于昨天平仓；第二批刚刚买入 1 BTC，规则为持有 86400 秒。调用真实 `TradingService._exit_rules()`，拦截 `_submit_draft` 后发现它立即提交当前全部 1 BTC 的退出草稿，触发来源仍为第一批 intent。

源码因果链：

- `src/bybitbot/service.py:1615` 查询全部历史开仓意图，`1628` 的 owners 只按策略、合约及存在退出规则过滤，没有排除已完全卖出的批次。
- `src/bybitbot/service.py:1657` 读取最新买入时间；`1672` 在有新买入时开始新一轮退出判断。
- `src/bybitbot/service.py:1676` 遍历历史 owner，`1679` 取该 owner 最早成交时间，`1691` 用该时间判断持有期是否已到。
- `src/bybitbot/service.py:1732` 对当前全部聚合库存计算退出数量，而非触发规则对应的仍持有数量。
- `src/bybitbot/service.py:1772` 将退出草稿送往下单管线。

影响：定期买入并持有的策略在第一个完整买卖周期之后可能退化成“刚买即卖”，偏离持有期配置并重复产生手续费。即使接受“按策略/合约聚合库存”的设计，已经完全平仓的历史 owner 也不应持续驱动新一轮库存的持有计时。

验证边界：真实业务方法被调用；查询结果由内存 fixture 提供，提交方法为 `AsyncMock`。这证明给定该合法历史状态时的业务判断结果，未验证数据库落库或线上实际成交。建议修复批次归属或明确的库存 episode 起点后，增加“全部平仓→新买入→未满持有期不退出”的集成回归。

## CR-02 · P1 · 期权模拟手续费低估，预算及止盈判断沿用同一费率

`src/bybitbot/adapters/paper.py:73` 默认 `option_taker_rate=0.0015`，`457`～`484` 使用期权权利金 × 数量 × 费率计算费用；默认每张额外费用为零。示例配置 `config/config.example.yaml:19` 同样为 0.0015。

Bybit 当前标准期权非 VIP taker 公式为 `min(0.03% × 标的指数价格, 7% × 期权成交价) × 成交数量`，账户层级可能改变费率或封顶比例。来源：[Bybit 期权手续费说明](https://www.bybit.com/en/help-center/article/Bybit-Option-Fees-Explained)（页面更新于 2026-08-19，本次于 2026-11-01 核对）；账户费率接口见 [Get Fee Rate](https://bybit-exchange.github.io/docs/v5/account/fee-rate)。

纯内存调用真实 `PaperAdapter._apply_fill()`：标的指数 100000、期权成交价 100、数量 1，模拟费用 **0.15 USDT**，上述官方非 VIP taker 公式为 **7 USDT**，约为模拟值的 **46.67 倍**。这是演示公式差异的合成输入，不是用户账户费率核定或真实成交。

影响不只在 paper 展示：`src/bybitbot/service.py:1086`、`1111` 的策略 sizing，`1296` 的预算校验，以及 `1705` 的止盈净收入估算均引用 `config.paper` 费率，没有按 active environment 区分。因此 paper 收益和实盘交易前预算、止盈判断均不能据此视为可靠。账本成交后使用交易所实报费用是另一路径，不能纠正此前基于错误估计作出的决定。

建议：以明确版本的官方公式和经核验的账户费率、指数价、费率上限计算估计费用；保守兜底也应以封顶公式为依据。实际成交仍使用交易所费用事实入账。

## CR-03 · 上线阻塞 · 真实期权到期交割尚未接入

- `src/bybitbot/service.py:613` 的结算处理只接受 `PaperAdapter`，非 paper 在 `615` 直接返回。
- `src/bybitbot/adapters/bybit_rest.py:1027` 拒绝非 `Trade` execution，提示需要单独结算记账。
- `src/bybitbot/service.py:1640` 遇到已到期期权仍有库存，产生 `SETTLEMENT_PENDING` 并保留成本。
- 项目验收文档 `docs/acceptance.md:90` 明确真实 Bybit 交割解析尚未接入。

影响：到期的真实期权不能由现有链路完成结算、成本释放与权益闭环。保留成本是保守处理，但不是完整可长期运行的交割能力。真实 Demo/Testnet 的成交、撤单、权限、单位和交割仍需分别验证。

## 其他已核实边界

- `README.md:3` 明确当前交付为 paper 实现，尚无实盘发单资格；`docs/acceptance.md:15` 自报 28 条验收中 0 全验证、25 部分验证、3 待验证。这是仓库文档记录，不是本次重新完成全部验收。
- `src/bybitbot/service.py:216` 对 live 检查启用门槛、通知渠道和风险起点；`261` 按环境分开 paper/真实适配器。保护存在，但不能代替实际非实盘联调。
- 风控具备身份、执行资格、未知订单、负债、信号/行情时效、预算、价差、敞口及 Delta 检查（`src/bybitbot/risk/engine.py:162` 起）。减仓另走判定分支（`244` 起）。本次只读核查没有重新证明所有组合场景。
- 模板仅为定额现货买入（`src/bybitbot/strategy/templates.py:87`）与单腿期权买入（`141`），后者按最低权利金、到期、合约名排序选择（`152`），不能据此推导存在盈利优势。
- 自动 Delta 对冲、永续/期货、资金费率套利及完整历史期权回测均不在当前范围（`docs/requirements.md:38`）。未实现资金费率链路与本项目“现货+标准期权”范围一致，不宜单列为漏算永续资金费的 bug。
- 项目本身明确“策略盈利不属于系统可靠性验收指标”（`docs/requirements.md:11`）。接口连通与模拟成交都不能证明净收益。

## 可复跑证据

在仓库根目录运行：

```sh
.venv/bin/python dogfood-output/2026-11-01/reproduce_readiness.py
```

脚本只构造内存对象；退出提交被 mock 截获，费用通过 PaperAdapter 内存账务计算。没有远程适配器、真实凭据、数据库连接或下单行为。

预期观察输出（日期计算不影响关键值）：

```json
{"scenario":"old_closed_purchase_triggers_exit_of_new_inventory","new_inventory_age_seconds":0,"configured_hold_seconds":86400,"exit_submitted_to_mock":true,"exit_quantity":"1.000","trigger_owner_intent":1}
{"scenario":"option_fee_example_non_vip_taker","index_price":"100000","option_price":"100","quantity":"1","paper_fee_usdt":"0.1500","official_formula_fee_usdt":"7.00","fee_multiple":"46.66666666666666666666666667"}
```
