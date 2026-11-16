# 期权筛选与风控收紧（2026-11-02）

适用环境：NAS paper（`bybitbot-worker` / `bybitbot-web`）。
发布标签：`localhost/bybitbot:option-controls-20261102`（镜像 ID `a9bc0bca0a09`），
已移动为 `localhost/bybitbot:0.1.0`；回滚点 `localhost/bybitbot:pre-option-controls-20261102`
（镜像 ID `5ea93d176eca`）。配置备份 `~/.config/bybitbot/config.pre-option-controls-20261102.yaml`。

本轮**没有解除任何实盘门禁**：策略仍为 `enabled: false`，三条暂停未解除，
基线未建立，告警通道仍为空，账户仍是只读密钥。

## 1. 依据：真实账户复盘

主账户全部成交（22 笔，10 个订单，全部成交）：

| 项目 | 数值 |
| --- | --- |
| 期权净盈亏 | −417.62 USDT |
| 其中手续费 | 60.87 USDT（占权利金 2.50%） |
| 价格损失 | −356.75 USDT |
| 退出方式 | 全部为市价 taker，无一笔挂被动价退出 |
| 止损 | 无 |

逐笔复盘得到四条可直接落到参数的结论：

1. **买入的是彩票型合约**：DTE=2、delta 0.337 的合约持有 3.5 小时亏 23%；
   DTE=22 的 BTC put 持有 20.8 小时亏 44%，全程无止损。
2. **费用吃掉权利金**：实测每边 taker 费用占权利金 1.61%～2.09%，双边 2.50%。
   权利金越便宜，费用占比越高（费用 = min(指数价 × 0.0003, 权利金 × 0.07)）。
3. **没有持仓上限**：账户在频繁划转，仓位规模与权益没有挂钩。
4. **退出只靠时间**：到期前才被动平仓，价格已经走完。

## 2. 参数改动

`eth-call-weekly`（`config/config.example.yaml` 与 NAS `~/.config/bybitbot/config.yaml` 同步）：

| 参数 | 旧值 | 新值 | 依据 |
| --- | --- | --- | --- |
| `option.min_days_to_expiry` | 1 | 7 | 不碰临近到期的合约 |
| `option.delta_range` | null | [0.30, 0.60] | 排除深度虚值；目标 Delta 0.45 |
| `option.strike_range_pct` | null | [90, 110] | 只在平值附近交易 |
| `option.max_fee_ratio` | 无 | 0.01 | 每边费用 ≤ 权利金 1%（双边 ≈1.6%） |
| 策略 `max_fee_ratio` | 无 | 0.01 | 同上，单实例可覆盖全局 |
| 策略 `min_premium` / `max_premium` | 无 / 无 | 50 / 200 USDT | 排除彩票价与过贵合约 |
| 策略 `delta_low` / `delta_high` | 无 | 0.30 / 0.60 | 与全局一致，单实例可覆盖 |
| 策略 `min_days_to_expiry` | 无 | 7 | 同上 |
| 退出规则 `premium_stop_loss` | 无 | 25% | 权利金亏 25% 即退出 |
| 退出规则 `hold_period` | 无 | 1209600 秒（14 天） | 兜底持有期限 |
| 退出规则 `before_expiry` | 24 小时 | 保留 | 到期前退出 |
| `risk.premium_cost_limit_per_underlying.ETH` | 未设置 | 20 USDT | 单标的权利金成本上限 |
| `risk.delta_limit_per_underlying.ETH` | 10.0 | 1.0 | 名义敞口上限（10 倍收紧） |
| `risk.max_premium_to_equity_ratio` | 无 | 0.2 | 权利金总额 ≤ 权益 20%，权益下降自动收紧 |

`options_type: Call` 显式写入策略参数（全局 `allowed_option_types` 已只有 Call，
显式写出避免全局配置变化时策略类型跟着变）。

### 选合约规则也改了

原规则按权利金从低到高排序 —— 最便宜的合约恰好是临近到期、深度虚值的彩票。
新规则：**先按 Delta 接近度排序**（`|delta − 0.45|` 最小），再按权利金、到期日、
合约名排序。费用占比上限用指数价自适应：ETH 指数价 2312 时，每边费用 0.69 USDT，
权利金低于 69 USDT 的合约一律不选。

## 3. 代码改动

| 文件 | 改动 |
| --- | --- |
| `src/bybitbot/strategy/templates.py` | `OptionBuyParams.max_fee_ratio` / `prefer_target_delta`；`_candidates` 过滤费用占比；`evaluate` 按 Delta 优先排序并记录 `sort_rule`；`ExitRuleParams.premium_stop_loss` |
| `src/bybitbot/settings/models.py` | `OptionConfig.max_fee_ratio`；`ExitRuleConfig` 新增 `premium_stop_loss` 与 `stop_loss_pct`；`StrategyConfig` 期权参数白名单扩到 11 项并逐项校验；`RiskLimits.max_premium_to_equity_ratio`；`premium_stop_loss` 只允许用于 `option_single_leg_buy` 策略 |
| `src/bybitbot/risk/engine.py` | `RiskContext.risk_equity`；新增 `PREMIUM_EQUITY_RATIO` 检查（权益不可用时拒绝开仓） |
| `src/bybitbot/service.py` | `_scheduled_strategy` 允许策略参数覆盖到期窗口、Delta 区间、行权价带、费用上限；`_exit_rules` 实现权利金止损触发；`RiskContext` 传入风控权益 |
| `src/bybitbot/web/app.py` | 配置版本载荷新增白名单化的 `params` 与 `exit_rules` |
| `src/bybitbot/web/templates/console.html`、`static/console.js` | 操作台「策略与风控」新增「策略生效参数」表（生效参数 + 退出规则），限额表新增期权到期窗口、Delta 区间、行权价带、费用占比上限、权利金占权益比例 |

## 4. 验证

* 单元与集成测试：`pytest` 400 项通过（原 379 项 + 新增 21 项），`ruff check src tests research` 通过。
  新增用例覆盖费用占比过滤（通过/拒绝/缺指数价）、Delta 优先排序、参数校验、
  权益比例限额（超限拒绝、权益不可用拒绝）、权利金止损触发退出，
  以及权利金止损不能挂在现货策略上（现货按买一价判定权利金成本，永远不触发，
  在配置期拒绝比运行时静默失效更早暴露）。
* 部署：镜像 `option-controls-20261102` 构建成功，`config check` 通过
  （两个策略 `enabled: false`、`live_allowed: false`）。
* 运行状态：`state=PAUSED_OPEN`、`tick_at` 正常更新、无 `last_error`；
  `order` / `execution` / `trade_intent` / `inventory` 全部为 0。
* 配置版本：`eth-call-weekly` 与 `btc-daily-spot` 生成 v2（v2 生效），
  `eth-call-weekly` 的 v2 载荷带新参数与新退出规则；暂停记录新增
  `CONFIG_CHANGED`（锁定，等待人工放行）。
* 操作台：`/api/status` 载荷实测含新参数、新退出规则与新限额；
  页面与静态资源由新镜像提供（`strategy-params-body`）。
  2026-11-04 复核补丁把退出规则的展示收敛到业务字段
  （`stop_loss_pct=25`、`hold_seconds=1209600`），重试次数与有效期等执行细节
  不再淹没阈值；`tests/console_ui.test.mjs` 新增一例断言只渲染生效版本、
  参数与退出规则均落到页面上（node --test 6 项通过）。
* 实盘行情核对（只读公开接口，不下单）：同一时刻 ETH 全部 229 个 Call 合约中

  | 规则 | 候选 | 最便宜的一个 | 每边费用占权利金 |
  | --- | --- | --- | --- |
  | 旧参数（最便宜优先） | 15 | ETH-4NOV26-2360-C，到期 **2.2 天**，delta 0.32 | **3.35%**（双边 6.7%） |
  | 新参数 | 2 | ETH-20NOV26-2350-C，到期 18.2 天，delta 0.47 | **0.78%**（双边 1.6%） |

  新规则实际会买入 `ETH-20NOV26-2350-C`：0.2 张 × 88.9 USDT = 17.78 USDT 权利金
  加手续费，落在 20 USDT 单笔上限内。

## 5. 仍未解除的门禁

本轮只改参数与风控，没有触碰下列任何一项：

* `eth-call-weekly` / `btc-daily-spot` 仍是 `enabled: false`，需人工启用（ENV-03）。
* `CONFIG_CHANGED`、`VALUATION_UNAVAILABLE`、`NOTIFICATION_FAILURE` 三条暂停未解除。
* 风险基线未建立；`alert_channels` 仍为空（告警发不出去，`NOTIFICATION_FAILURE` 无法自愈）。
* 真实账户仍用只读密钥，且该密钥曾出现在会话记录里，**需要轮换**；
  轮换前先暂停开仓并核对在途订单。
* 账户有频繁划转：`REGISTER_EXTERNAL_CASH_FLOW` 未登记的资金变化会被判为
  无法解释并触发严重暂停；启用前先把外部划转登记干净。
