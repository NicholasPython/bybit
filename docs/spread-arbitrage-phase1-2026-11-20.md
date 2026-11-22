# 双腿价差套利（BLSAE）阶段一交付与 P0 阻断项修复（2026-11-20）

**分支**：`feat/spot-lab-module`　**基线**：`9de12a3`　**回归**：1723 项通过（`uv run pytest -q`），`ruff check src tests deploy alembic` 全绿

本轮交付两件事：

1. **P0 阻断级缺陷全部修复**（差距评估 §2.1 / §2.2 / §3.2 / §3.3 的第 0 阶段）—— 实盘写路径此前每一笔单都会被判「结果未知」；
2. **BLSAE 阶段一工程底座**（PRD §3 第一阶段工程开发）—— DDL、配置模板、双腿原子执行与「瘸腿自愈」状态机。

---

## 一、为什么先修 P0 而不是直接盖新功能

PRD 的目标是「可长期无人值守运行 + 双腿精准对账」。但实盘适配器与订单状态机的
**契约错配**让实盘根本无法开仓：

```python
# 修复前：bybit_rest.py 回执措辞
status="Accepted"          # 不在 states.py 的归一化词表里
```

`effective_status()` 把不在词表里的词一律归一为 `UNKNOWN`，于是**每一笔实盘单**
都会走 `UNKNOWN → 保留预留 → 锁定暂停` 的路径，账户永久无法开仓。模拟适配器恰好
返回 `"New"`，把这个错配完整掩盖了 —— 这也是 1411 项测试全绿却仍有实盘缺陷的原因。

先修这条链，后面写的双腿状态机才有意义。

---

## 二、P0 修复清单

| # | 改动 | 位置 | 回归测试 |
|---|---|---|---|
| 0-1 | 回执措辞改用词表内的 `"New"`；`effective_status` 额外收录 `"accepted"` 作防御 | `adapters/bybit_rest.py`、`execution/states.py` | `test_live_ack_vocabulary_never_becomes_unknown`（参数化 `New`/`Accepted`/`Created`）、`test_spot_no_leverage_and_signed_json_bytes` |
| 0-2 | `ORDER_STATUSES` 补 `"Created"`（V5 条件单态） | `adapters/bybit_rest.py` | `test_created_order_status_is_recognised` |
| 0-3 | 挂单查询 `openOnly: 0 → 1` | `adapters/bybit_rest.py` | `test_open_order_query_asks_only_for_open_orders` |
| 0-4 | 写侧接入限流节流：发送前读冷却窗口；HTTP 429 与限流错误码判为**确定未受理**（`OrderRejected`）而非 `OrderUncertain` | `adapters/bybit_rest.py` | `test_rate_limited_write_is_a_definite_rejection`（3 个参数）、`test_write_waits_out_a_short_rate_limit_cooldown_before_sending`、`test_write_refuses_to_send_during_a_long_rate_limit_cooldown` |
| 0-5 | 备份覆盖三个库、逐份 `pg_restore --list` 校验、轮转；Mac 侧改为**解密校验**而非看文件大小 | `deploy/backup.sh`、`deploy/watch_nas.py`、`deploy/install_watch.py` | `test_backup_problems_cover_missing_stale_and_unverified`、`test_backup_inventory_is_per_database_and_reports_verification`、`test_backup_file_name_parsing` |

### 0-4 的关键判断

限流发生在**创建订单之前**：交易所网关拒绝请求，订单没有被创建。因此限流是
「确定未受理」，走既有的 `_mark_rejected` 路径（释放预留、不锁账户），而不是
「结果未知」。此前两者混为一谈，交易所限频一次就足以让实盘永远无法开仓。

冷却窗口**不**复用查询侧的「等到限流窗口重置」逻辑 —— 那会把下单串行化到
1 次/秒，直接毁掉双腿对冲的延迟预算。写侧有独立的冷却状态，只在真的被限流时写入。

### 0-5 的关键判断

原备份检查只看 `st_size > 0`，一个截断或损坏的备份会被判定为正常。现在 Mac 侧
（持有私钥）**解密备份**并检查 pg_dump 魔数 `PGDMP`：

- 解密成功即证明密文通过了 AEAD 完整性校验；
- 明文必须是 pg_dump 归档，否则不是一份能恢复的备份；
- 截断样本实测 `gpg` 返回码 2、垃圾数据同样返回 2 —— 两者都会被判为不合格。

实测证据（仓库内真实备份）：

```
$ gpg --decrypt bybitbot-20261118T060507Z.dump.gpg | head -c 5
PGDMP
$ head -c 100000 … > trunc.gpg && gpg --decrypt trunc.gpg   # rc=2
```

---

## 三、BLSAE 阶段一

### 3.1 数据模型（PRD §5）

新增迁移 `3f7c1d92ab40_spread_arbitrage`，四张表：

| 表 | 作用 | 关键约束 |
|---|---|---|
| `spread_pair_config` | 套利币对配置（β、Z 阈值、资金上限） | `environment+pair_key` 唯一；`z_exit < z_entry < z_stop`；裸敞口上限 < 单组上限 |
| `spread_pair_trade` | 一次配对生命周期 | `environment+pair_trade_id` 唯一；状态枚举受约束 |
| `spread_leg_order` | 腿订单明细 | `environment+order_link_id` 唯一（幂等）；`0 ≤ qty_filled ≤ qty` |
| `spread_reconciliation_log` | 准实时对账快照 | 状态枚举；`difference ≥ 0` |

迁移与模型的一致性由既有的迁移往返测试保证（`upgrade → check → downgrade → upgrade → check`，
`alembic check` 要求模型与迁移零差异）。

**与既有 `order` 表的关系（重要边界）**：套利腿**不进入**既有 `order` 表。既有表以
`trade_intent` 为主键外键，而套利的下单单位是「一次配对」——两条腿共享同一个
`pair_trade_id`、按配对占用资金与杠杆。拆成两个意图会让预留与限额各算一半，
逐意图风控口径与配对级口径会打架。

由此产生一条**必须记录在案的边界**：线性腿走 `category=linear`，而既有对账只覆盖
`spot`/`option`，因此**线性腿的对账由 `spread_reconciliation_log` 负责**；且在 UTA
账户下，线性保证金变动会与既有现金对账的「外部资金流」判定冲突。接入实盘前必须
先解决这一条（差距评估 §4.3「保证金占用风控」与 §5.2）。

### 3.2 状态机与不变量（`spread/states.py`）

```
OPENING ──双腿建立──> HEDGED ──发起平仓──> CLOSING ──双腿了结──> CLOSED
   │                      │                    │
   │                      │                    └──裸腿无法修复──> ERROR_UNHEDGED
   ├──Leg A 超时零成交──> ABORTED
   └──裸腿无法修复────────> ERROR_UNHEDGED
```

三条硬不变量，与 `execution/states.py` 同一口径：

1. **终态不可倒退** —— `CLOSED`/`ABORTED`/`ERROR_UNHEDGED` 之后不再迁移，晚到事实只更新账务；
2. **裸敞口有界** —— 进入 `HEDGED` 时名义差必须 ≤ 上限，越界的配对只能进 `ERROR_UNHEDGED`；
3. **成交数量单调不减** —— 旧快照不倒退已确认成交（`max()` 夹住，而非抛错）。

对冲量按 PRD 的**等名义价值**原则：`qty_b = 名义 / price_b`，再**向下**取整到数量步长
—— 宁可少对冲，不要多出一条敞口。

### 3.3 配对级硬风控闸门（`spread/risk.py`）

纯函数，一票否决，**所有条目都要给出结论**（静默通过一条、悄悄拒绝另一条是风控
最危险的失效方式）。覆盖 PRD FR-03 的五个维度：

| 维度 | 判定 | 阈值 |
|---|---|---|
| 维持保证金率 | > 50% 禁止新开仓；> 75% 触发等比例减仓 | FR-03.3 |
| 单组名义占净值 | > 30% 拒绝 | FR-03.3 |
| 全账户总杠杆 | > 3x 拒绝 | FR-03.3 |
| 单腿裸敞口 | 名义差 > 50 USDT 且持续 > 5s → 平掉多余敞口 | FR-03.1 |
| 价差止损 | \|Z\| ≥ 3.5σ → 双边止损 | FR-03.2 |
| 资金费率逆风 | 净支出 ≥ 预期利润 50% → 提前了结 | FR-03.4 |
| 发单频率 | 同配对 10 秒内 > 15 次 → 拒绝 | FR-03.5 |

### 3.4 双腿原子执行与瘸腿自愈（`spread/execution.py`）

用**事件归约**表达执行流程：`reduce(execution, event)` 是纯函数，返回新状态与一组
待执行动作。这样双腿时序（先弱后强、部分成交、超时、追单、反向平仓）全部可以
单测复现，且换执行后端不需要改状态机。

时序：

1. 腿 A（流动性较差的一腿）挂 Post-Only 限价单；
2. 腿 A 一有成交（哪怕部分成交）立即按等名义价值计算腿 B 数量，以 IOC 保护价发单；
3. 腿 A 超时零成交 → 撤单、放弃本次开仓（`ABORTED`）；
4. 腿 B 的 IOC 未完全成交 → 恢复窗口内追单补偿；
5. 追单滑点超出容忍度 → 立即反向平掉腿 A 的已成交头寸，转 `ERROR_UNHEDGED`；
6. 裸敞口持续超限 → 平掉多余敞口（名义大的一侧），转 `ERROR_UNHEDGED`。

数量口径上，一条腿可能由**多笔物理订单**组成（首单 + 若干次追单），因此区分
`qty`（当前物理订单）、`qty_target`（该腿累计目标）、`qty_filled`（交易所累计成交）。

幂等：订单编号 `{pair_trade_id}_{leg}{序号}`，长度 ≤ Bybit 的 36 字符上限；**同一笔
逻辑订单的所有重试必须复用同一个编号**，序号只在产生新逻辑订单时递增。

### 3.5 配置与网关

- `config/config.example.yaml` 新增 `spread:` 段（默认整体关闭，含阈值校验与样例币对）；
- `settings/models.py` 新增 `SpreadSettings` / `SpreadPairSettings`，非法组合在启动时被拒绝；
- 适配器新增 `category=linear` 支持：`instruments-info` 对 linear 不再发送只对期权有效的
  `baseCoin=All`；下单走一号仓位模式（`positionIdx=0`）+ `reduceOnly`，不带现货的 `isLeverage`。

---

## 四、明确没有做的事（能力边界）

1. **私有 WebSocket 成交流已接入事件适配层，但没有真实账户实测，不能宣称已达成 15 毫秒指标。**
   本轮在 `adapters/bybit_ws.py` 中扩展了 `linear` 私有 `order` / `execution` 订阅、
   鉴权与心跳超时处理，并在 `spread/events.py` 中新增纯适配层：把交易所推送翻译成
   `PairEvent` 后仍交给 `spread.execution.reduce()`，**状态机没有改成 I/O 状态机**。
   缓存去重、单调累计和校准边界在适配层完成；断线/鉴权失败/心跳超时/漏流迹象
   立即进入 fail-closed，必须由 REST 对订单、成交、仓位三维做全量校准后才恢复。
   当前尚未接入持久化执行器与账户级实时风控，因此这只是"私有事件到归约器"的通路，
   不是端到端实盘对冲闭环；15ms 指标仍需 paper→影子盘→灰度实测后才可宣称。
2. **没有做影子盘、灰度晋升与周度寻优**（PRD FR-07）。离线进化平面允许下发**合规参数**，
   不允许直接改实盘代码 —— 这条边界本期保持不变。
3. **没有接入实盘**。`spread.enabled` 默认 `false`，配对默认 `enabled: false`，
   且既有 live 三道门（`live_allowed` / `acceptance_record_ref` / `manual_enable_ref`）不受影响。
4. **没有做套利腿的实盘对账与归因**（PRD FR-05）—— 需要线性腿的持久化执行器，
   属于阶段二。
5. **没有改写既有现货/期权的账本与对账**，避免把一个 1723 项测试覆盖的系统拖入不稳定。

---

## 五、建议的下一阶段顺序

| 顺序 | 事项 | 理由 |
|---|---|---|
| 1 | 套利腿的持久化执行器 + 线性腿对账（`spread_reconciliation_log` 落库） | PRD FR-04.3 / FR-05；私有事件到动作已经打通，缺的是动作落地与账户级实时风控，也是 UTA 保证金与既有现金对账冲突的解法 |
| 2 | 配对信号层（协整检验、Z-Score 实时计算、Walk-Forward 寻优） | PRD FR-01.3 / FR-07.1 / FR-07.2；离线侧，不碰实盘代码 |
| 3 | 影子盘与灰度晋升 | PRD FR-07.3；必须等 1 完成 |
| 4 | 既有系统的长期运行地基（止损与租约解耦、日志轮转、进程看门狗、限额改权益比例） | 差距评估第 1、2 阶段。与 BLSAE 无关，但同样阻断「敢接真钱」 |

第 4 项与 BLSAE 平行推进：它修的是**既有现货/期权产品**的实盘门槛，
BLSAE 修的是**新产品**的能力。两者互不阻塞，但都排在「实盘」之前。
