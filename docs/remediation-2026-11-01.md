# 2026-11-01 测试报告整改与发布记录

依据本机 `dogfood-output/2026-11-01/report.md` 及 `code-review.md`。原报告和截图保留为修复前证据，不改写当时观察；本记录说明整改后的行为及验证范围。

## 问题处理

| 报告编号 | 处理结果 | 验证证据 |
|---|---|---|
| CR-01：旧买入持有期触发新库存立即退出 | 新增库存持有周期字段。实际成交使库存从零变正时保存成交时间和买入意图；同周期追加买入保留起点，全平后的新买入重新计时。退出只读取当前周期归属。旧库存缺少归属时保持待核实，不猜测回填。 | PostgreSQL 服务集成覆盖完整买入→退出成交→库存归零→重新买入→持有期未满不退出，以及追加买入不重置起点；原内存复现输出 `exit_submitted_to_mock=false`。 |
| CR-02：期权费用估计偏低 | 顶层 `fees` 配置及共享 `FeeModel` 按 `min(index_price × 0.0003, option_price × 0.07) × qty` 估计，再加配置的每张额外费用；缺失/无效/过期指数价按封顶比例估计。模拟撮合、策略数量、预算和净止盈判断共用模型，真实成交按交易所实报费用入账。 | 费用测试 13 项、服务预算和止盈回归；报告示例指数价 100000、权利金 100、数量 1，费用由 0.15 改为 7 USDT。 |
| CR-03：真实期权到期交割未接入 | 新增只读交割事实接口。先处理普通成交/订单，再核对交割、持仓、现金、预留。交割独立写 SETTLEMENT 及非外部现金流，不伪造卖出成交。 | MockTransport 接口契约 116 项；数据库交割对账 9 项。真实 Demo/Testnet 返回及实际交割尚未验证，不能标为真实账户验收通过。 |
| ISSUE-001：确认词错误被写成结果未确认 | 确认词及表单错误在登记前处理，仅显示输入提示，保留填写内容并聚焦确认词；不保存未确认命令。真正网络不确定时保留原编号重试。 | Node 回归及实际本机浏览器：输入“测试”后提示为“请输入完整的‘确认’后登记命令。”；命令数量保持 1，未确认状态为空，重试按钮隐藏。 |
| ISSUE-002：已执行命令上方仍显示待处理 | 上方提示跟踪本次命令编号，每次刷新读取同一条持久化命令状态及最终结果。 | Node 回归及实际本机浏览器：空订单撤单命令由待处理转为已执行，上方同步显示结果 `pending=[]`、`terminal_confirmed=[]`。 |

自动退出仍按策略/合约聚合库存管理，未实现逐笔 FIFO。已有退出流程的原期限、数量及追价次数保持约束。

## 交割事实的保守边界

- 读取 `/v5/asset/delivery-record`，要求合约、方向、数量、行权价和到期时间一致，以标的交割价和行权价计算内在价值支付。
- 正支付必须有 `/v5/account/transaction-log` 的精确 DELIVERY 流水；核对结算币、数量、`cashFlow`、费用、`change` 和零 funding。不把 `deliveryRpl` 当作现金支付，也不假定 delivery-record 的 `fee` 是交割现金费用。
- 零支付缺少流水时，必须同时有零费用交割记录和 `/v5/position/get-closed-positions` 中数量、交割价、到期时间、零费用一致的独立证明。该回退仅接受 180 天内历史；普通提前卖出不能充当到期交割。
- 分页必须完整且游标不重复；查询按不超过七天的连续窗口划分。重复、冲突、未知币种、单位或缺字段均拒绝。证据仅保留白名单字段。
- 账户 UID、自有现金、完整期权持仓、策略库存数量及活动/未知订单均须核对。所有候选交割净现金与到期前可信快照重放结果一致后才释放成本；失败期间保存的不完整快照不能吸收未解释现金变化。
- 证据不足时继续保留库存成本并记录 `SETTLEMENT_UNVERIFIED`，后续轮询可再次核对。真实零支付是否产生上述记录仍需受限 Demo/Testnet 验证。

## 本地验证

- 完整 Python 回归：**340 passed in 27.01s**（含本轮真实账户缺陷的 5 项新增回归）。
- 页面命令提示：`node --test tests/console_ui.test.mjs`，**4 项通过**。
- `ruff check src tests deploy alembic`、`git diff --check` 通过。
- 本地 Alembic head 为 `8a4d17c2f905`，`alembic check` 无新增操作。
- 报告复现脚本输出：新库存不提交退出；期权费用与官方公式示例同为 7 USDT。

自动化交易所 HTTP 使用 MockTransport，默认 HTTP transport 被阻断；数据库测试为独立随机 schema。实际浏览器运行本机 FastAPI 管理页，使用临时合成身份及本机 paper 数据，完成后退出并删除临时身份。为验证提示更新，只通过控制管理器执行本次本机的空订单撤单命令 `891e32cb-fb57-46c1-ae7c-7763efcf08f3`；本机原有待处理命令未被领取，NAS 未新增业务命令。浏览器无控制台错误或失败请求。

## NAS 发布结果

2026-11-01 04:00 UTC 发布（上海时间 12:00 左右）：

| 项目 | 实际结果 |
|---|---|
| worker / web 镜像 | `474fbae0c7c501d3dd7fca2ec996c1536bc3c3c2c9b1549c87215eb5dcb7151f`，标签 `localhost/bybitbot:dogfood-20261101`、`localhost/bybitbot:0.1.0` |
| 文件一致性 | 候选镜像中 53 个应用、页面、迁移与配置示例文件逐一 SHA-256 与本机一致；worker/web 均使用同一 image ID |
| 数据库 | head `8a4d17c2f905`；持有周期字段不回填历史猜测值 |
| 费用配置 | 顶层 `fees`、版本 `bybit-fee-v1`；原配置 0600 备份 `~/.config/bybitbot/config.pre-dogfood-20261101.yaml` |
| paper 检查点 | 发布前订单、成交、正库存均为 0；旧费用元数据安全迁移，保留余额 1000 USDT；原检查点存于 `paper-fee-migration` 审计记录 |
| 业务状态 | `PAUSED_OPEN / NEEDS_CONFIRMATION`；两策略 false；基线未建立；订单、成交及正库存均为 0；`last_error` 为空 |
| 暂停 | 原 `VALUATION_UNAVAILABLE`、`NOTIFICATION_FAILURE`、`CONFIG_CHANGED` 保持活动，未代为解除 |
| 进程和互斥 | PostgreSQL、worker、web、备份 timer 均 active；实际 granted advisory lock 1 把；worker 心跳持续更新 |
| HTTPS | `https://nicholas-4.tail068b64.ts.net/health` 返回 `{"status":"ok"}` |
| 回环监听 | PostgreSQL `127.0.0.1:55432`、web `127.0.0.1:18080` |
| 备份 | 发布前加密备份 `bybitbot-20261101T035525Z.dump.gpg` 成功生成并复制到 Mac 异机备份目录，0600 |
| 回滚镜像 | 旧镜像 `c1d89f9541d9b86f8b67ee173933b73f86808c48f135db95e1a55475937de049` 保存在 `localhost/bybitbot:pre-dogfood-20261101` |

回滚必须同时核对旧配置格式与检查点兼容性；发布后有交易事实时不能直接恢复旧检查点覆盖新账本。详情见 [NAS 手册](runbook-nas.md)。

未配置真实业务通知渠道，未接入交易凭据，未启用策略、建立基线、确认接管或解除暂停。真实成交、撤单、交割、账户费率和阶段验收均不能由本轮合成测试替代；验收矩阵仍为 0 项全部验证、25 项部分验证、3 项待验证。

## 5. 真实账户只读核验（2026-11-01 晚）

用户提供了一把真实 Bybit key 用于测试。凭据只经受限文件与 stdin 管道送入 NAS 容器内的探测进程，脚本本身不含密钥；全程仅 GET，未产生任何订单、持仓或预留。

### 账户与权限事实

| 项目 | 实际结果 |
|---|---|
| 账户 UID | `587220933`（`/v5/user/query-api` 的 `userID`） |
| 主/子账户 | **子账户**：`/v5/account/info` 的 `isMasterTrader=false`，`/v5/account/sub-account/list` 返回 HTTP 404（子账户 key 无权调用主账户接口）；`inviterID=586413070` |
| 密钥类型 | 主网个人 key，`readOnly=1`（只读），提现关闭（`withdraw_enabled=false`） |
| 权限集合 | SpotTrade、OptionsTrade、DerivativesTrade、Order、Position、ByXPost 等 |
| IP 限制 | **无**（`ip_restrictions` 为空），不满足 SEC-01 的固定出口 IP 要求 |
| 会员等级 | `No VIP`（与 CR-02 使用的非 VIP taker 公式一致） |
| 账户模式 | `REGULAR_MARGIN`，`unifiedMarginStatus=5`（统一账户），`dcpStatus=OFF` |
| 净值 | 约 0.0201 USDT：USDT 0.00607、USD 0.00921、SOL 0.00005005；无负债；USDT 累计已实现盈亏 -421.75 |
| 持仓与挂单 | 期权、现货持仓均为 0，活动订单为 0 |

### 费率核对（支持 CR-02 的公式假设）

`/v5/account/fee-rate` 实测：期权 taker `0.0003`（BTC/ETH/TSLA/QQQ 一致）、maker `0.0002`～`0.0003`；现货 taker/maker `0.001`。与配置的 `option_underlying_rate=0.0003`、`spot_taker_rate=0.001` 及权利金 7% 封顶公式一致。

### 暴露的缺陷：钱包余额读取被阻断

真实响应中非保证金币种 USD 的 `accruedInterest` 为空串 `""`（同样为空的还有 `availableToBorrow`、`availableToWithdraw`），而适配器用 `_number(..., nonnegative=True)` 严格解析，空串即抛 `Missing or invalid accruedInterest`，**整个账户读取失败**。这是真实账户可用性的阻塞缺陷，会使身份核对与后续对账无法进行。

修复：新增 `_accrued_interest()`——仅当币种被交易所明确标记为非保证金（`marginCollateral=false`）且返回空串时按 0 处理；保证金币种返回空串、字段缺失、无法解析或为负时一律 fail-closed 拒绝读取。新增 5 项回归：空串非保证金币种可读且 USDT 可用余额不受影响；保证金币种空串、缺 `marginCollateral`、非数字、负值四种情形均拒绝。

### 未覆盖与前置条件

- 该 key 为只读，**不能下单**，因此期权下单、reduceOnly、成交回报、撤单与到期交割仍无真实账户证据；CR-03 的真实交割链路只能在 Demo/Testnet 或可写 key 上验证。
- 账户净值不足 0.02 USDT，任何真实下单验证都需先充值；而子账户充值/划转当前会被判为 `EXTERNAL_CASH_CHANGE` 触发 CRITICAL 暂停——缺少“登记外部资金流”的管理命令，需在实盘验证前补齐。
- 正式凭据需要满足 SEC-01：只保留必要权限、关闭提现、绑定固定出口 IP；并按 ENV-03 另行取得实盘允许配置、阶段验收记录与人工启用。
- 本次提供的 key 已出现在对话记录中，测试后应在 Bybit 撤销并重新生成；后续凭据改由本机受限文件提供。
