# P0 验收证据：AC-22（容量与保留）、AC-26（时钟与调度）

核验日期：2026-11-17。环境：macOS（arm64）、Python 3.12、PostgreSQL 16（本地容器）、`paper` 模拟环境、合成行情、**无真实凭据、无交易所写请求**。

本轮目标是把验收矩阵里最后两条"待验证"里的 AC-22 与 AC-26 补成可重复执行的证据，并补上此前确实缺失的实现：**数据保留清理**此前只有配置项、没有实现。

## 1. 结论

| AC | 之前 | 现在 | 说明 |
|---|---|---|---|
| AC-22 · NFR-01/02/05 | 待验证 | **部分验证** | 容量边界、容量负载、延迟分布、CPU/内存、订阅量、磁盘增长、磁盘不足暂停、保留清理都有可重复证据；剩余子场景只在参考环境与长期实跑上成立（见第 6 节） |
| AC-26 · NFR-06/STR-02 | 待验证 | **已验证** | 前跳、回拨、偏差超限、暂停开仓、调度幂等、超时与退避不受墙钟影响、恢复仍需对账与解除条件，全部有确定性证据 |

本轮新增的实现（不只是测试）：

| 改动 | 位置 | 目的 |
|---|---|---|
| `ClockWatch` 时钟监控 | `src/bybitbot/ops/clockwatch.py` | 把跳变/偏差判定从服务里抽成可注入时间源的独立部件，测试能确定性复现跳变 |
| 时钟恢复闸门 | `src/bybitbot/service.py::_clock_recovery_ready` | **时钟恢复不再自动解除暂停**：必须同时满足身份、对账、基线、估值、执行权、无负债、无未知订单 |
| `CLOCK_JUMP` 锁定暂停原因 | `src/bybitbot/ops/pauses.py` | 时钟跳变属于锁定类，技术性重新同步不能解除，重启也不能解除 |
| `RetentionCleaner` 保留清理 | `src/bybitbot/ops/retention.py` | 补上 NFR-05 缺失的实现：只回收**未被决策引用**的原始行情，账务与审计记录一律不删 |
| 保留清理接入 tick | `src/bybitbot/service.py::_retention_tick` | 每日一次，结论写入运行状态 |
| `LatencyRecorder` 延迟分布 | `src/bybitbot/ops/metrics.py` | 记录"意图校验与持久化预留"与整个 tick 的耗时分布（NFR-02 要求"记录延迟分布"） |
| 首期容量校验 | `src/bybitbot/settings/loader.py::AppConfig` | 超过 5 个策略实例或 10 个现货交易对的配置直接拒绝 —— 超出首期容量必须另做容量及订阅验证 |
| 意图速率上限 | `src/bybitbot/ops/ratelimit.py` | 策略合计每秒最多 1 个意图（NFR-01）；超出的计划**延后**到下一个调度周期再提交，不丢弃、不排队补做 |
| 交易所偏差复测 | `ClockWatch.skew_due` | 偏差暂停未解除时按更短间隔复测，恢复能尽快确认 |

## 2. AC-26：时钟与调度证据

测试文件：`tests/test_clock_resilience.py`（19 项）。

### 2.1 跳变与偏差判定（确定性单元证据）

| 用例 | 断言 |
|---|---|
| `test_forward_and_backward_jumps_are_both_detected` | 前跳 3600s、回拨 1800s 都被识别，方向与幅度准确 |
| `test_wall_and_monotonic_together_do_not_look_like_a_jump` | 墙钟与单调钟同步推进不算跳变 |
| `test_jump_is_reported_once_and_does_not_repeat_every_tick` | 跳变只报一次；暂停靠暂停表保持，不靠重复告警 |
| `test_skew_measurement_uses_the_round_trip_midpoint` | 用读取前后中点抵消往返延迟，30s 偏差测得 30.0s |
| `test_skew_within_threshold_is_acceptable` / `test_skew_measurement_rejects_naive_exchange_time` | 阈值内不判超限；无时区的交易所时间直接拒绝 |
| `test_skew_schedule_respects_interval_and_recovery_interval` | 常规 60s 复测；存在偏差暂停时按 10s 复测 |

### 2.2 超时与退避不受墙上时钟影响

| 用例 | 断言 |
|---|---|
| `test_deadline_is_unaffected_by_wall_clock_jumps` | 墙钟前跳 1 小时、回拨 2 小时，`Deadline` 的剩余时间纹丝不动；只有单调钟推进才到期 |
| `test_backoff_is_deterministic_and_capped` | 退避序列可重复、有上限，负数尝试直接拒绝 |

### 2.3 暂停新开仓与恢复条件

| 用例 | 断言 |
|---|---|
| `test_clock_jump_pauses_new_exposure_and_stays_locked` | 跳变 → 状态机 `PAUSED_OPEN`、登记**锁定** `CLOCK_JUMP` 暂停并告警；新意图被拒（"暂停新增风险"），被拒意图仍留痕 |
| `test_clock_jump_pause_survives_restart_and_technical_resync` | 技术性重新同步不能解除锁定暂停；**重启后暂停仍在** |
| `test_clock_jump_requires_manual_resume_with_reconciliation` | 只能通过带审计的 `RESUME` 解除 |
| `test_exchange_skew_beyond_threshold_pauses_new_exposure` | 偏差 60s > 阈值 5s → 登记技术暂停 `CLOCK_SKEW`，暂停新开仓 |
| `test_skew_recovery_waits_for_reconciliation_conditions` | 偏差恢复但对账未通过时**不解除**暂停；对账恢复后才解除 |
| `test_clock_recovery_requires_every_release_condition` | 解除条件逐条可证：身份、对账、基线、执行权、估值、负债任一不成立即不解除 |
| `test_clock_skew_does_not_widen_the_request_window` | 偏差不会把请求超时或挂单有效期改大（NFR-06 禁止用放宽窗口掩盖问题）；意图有效期仍是"计划时刻 + 配置 TTL" |

### 2.4 调度不重复、不补做

| 用例 | 断言 |
|---|---|
| `test_missed_slot_is_skipped_instead_of_bought_back` | 错过宽限期的计划标记 `SKIPPED/missed`，不补买 |
| `test_same_utc_slot_is_planned_once_even_after_backward_jump` | 同一 UTC 计划时刻只登记一次，重复投递拿不到第二个任务 |
| `test_clock_jump_does_not_trigger_catch_up_buys` | 前跳 1 小时、回拨跨 UTC 日界线都不会产生新意图 |

### 2.5 证据文件

```
BYBITBOT_EVIDENCE_PATH=docs/evidence/2026-11-17-ac26-clock.json \
  .venv/bin/python -m pytest tests/test_clock_resilience.py -q
```

## 3. AC-22：容量、磁盘与保留证据

测试文件：`tests/test_capacity.py`（12 项）。

### 3.1 首期容量边界（NFR-01）

| 用例 | 断言 |
|---|---|
| `test_first_phase_capacity_configuration_is_accepted` | 5 个策略实例、8 个现货交易对、200 个候选期权合约可以通过校验 |
| `test_more_than_five_strategies_is_rejected` | 6 个策略实例被拒绝，错误信息指向 NFR-01 与"另做容量及订阅验证" |
| `test_more_than_ten_spot_pairs_is_rejected` | 11 个现货交易对被拒绝 |
| `test_option_candidate_limit_cannot_exceed_two_hundred` | 候选期权合约上限硬性为 200 |
| `test_scheduled_intents_respect_the_per_second_limit` | 同一秒内的第二个计划被**延后**（`schedule_run.status` 回到 `PLANNED`，`result.deferred=true`、原因"超过每秒意图上限"），并产生 `INTENT_RATE_LIMIT` 告警；窗口过去后重新调度时被补上，两个计划各留一个意图，前一个不被重放 |

### 3.2 容量负载、延迟分布、CPU/内存、订阅量、磁盘增长

`test_capacity_load_records_latency_within_the_baseline` 在首期容量配置上连续提交 15 个意图，记录：

* **延迟分布**：意图校验与持久化预留（意图落库 → 风控评估 → 预留 → 账本追加 → 提交事务），
  这段只含本地校验与数据库事务，不含交易所网络等待 —— 正是 NFR-02 的口径；
* **CPU / 内存**：进程 CPU 时间与峰值常驻内存；
* **订阅量**：深度盘口调用次数与批量期权报价调用次数；
* **磁盘增长**：关键表在负载前后的字节差。

实测结果见第 4 节。断言基线：**P95 ≤ 500ms**、最大耗时 ≤ 500ms、深度订阅数不超过"真正需要的品种"数量（候选目录、报价刷新与深度订阅分层，不对 200 个候选合约逐个取深度）。

### 3.3 磁盘不足暂停

| 用例 | 断言 |
|---|---|
| `test_disk_pressure_pauses_new_exposure_without_losing_records` | 可用空间 1GiB < 暂停阈值 5GiB → 登记 `DISK_PRESSURE` 暂停并停止新增风险；暂停期间账本、成交、订单、库存、意图**一条不少**；空间恢复后技术暂停自动解除 |
| `test_disk_warning_below_free_threshold_does_not_pause` | 10GiB 介于告警阈值 20GiB 与暂停阈值 5GiB 之间 → 只告警不暂停 |

### 3.4 保留清理（NFR-05，此前缺失）

| 用例 | 断言 |
|---|---|
| `test_retention_removes_expired_raw_quotes_but_keeps_decision_evidence` | 超过 7 天的**未引用**原始行情被删除；被决策引用的快照保留，`trade_intent.quote_snapshot_id` 不被静默抹掉（外键是 `ON DELETE SET NULL`，删了就等于悄悄丢证据） |
| `test_retention_never_deletes_accounting_records` | 清理前后账本、成交、订单、库存、意图行数完全一致 |
| `test_retention_is_idempotent` | 重复清理第二次删除 0 行 |
| `test_service_runs_retention_once_per_day_and_records_the_report` | 服务每日清理一次并把结果写入运行状态 |

保留策略边界（写进实现注释与本节）：

* `quote_snapshot` 中**未被任何交易意图引用**的行 = 原始行情 → 保留 `raw_market_data_retention_days`（默认 7 天）后删除；
* 被 `trade_intent.quote_snapshot_id` 引用的快照 = 决策证据 → 随审计记录保留，不删；
* 账本、成交、订单、意图、预留、风控决定、现金流、权益快照、账户快照、暂停、告警 → **一律不删**。

> 现状说明：产品链路只在**决策时**落一条行情快照（`_submit_draft` → `persist_snapshot`），
> 不保存全市场逐笔行情，因此"原始行情"表当前几乎全部是决策证据；清理器提供的是这条
> 边界的强制实现与回归保护，而不是当前已经在回收大量数据。

### 3.5 证据文件

```
BYBITBOT_EVIDENCE_PATH=docs/evidence/2026-11-17-ac22-capacity.json \
  .venv/bin/python -m pytest tests/test_capacity.py -q
```

## 4. 实测数字

由上面的命令生成，可重复；以下是本机本次运行的产物。

### 4.1 容量与延迟（`docs/evidence/2026-11-17-ac22-capacity.json`）

| 指标 | 实测 | 基线 |
|---|---|---|
| 容量配置 | 5 个策略实例、8 个现货交易对、200 个候选期权合约 | 首期上限 5 / 10 / 200 |
| 意图校验与持久化预留 | 15 个样本：P50 **33.8ms**、P95 **44.9ms**、最大 **78.6ms**、均值 37.0ms | P95 ≤ 500ms ✅ |
| CPU | 2.29s（每意图 0.153s） | 4 vCPU 参考环境下有余量 |
| 峰值常驻内存 | 108.5 MB | 8 GB 参考环境下有余量 |
| 期权报价 | 批量报价调用 **2** 次 | 分层订阅，不逐合约取深度 ✅ |
| 深度订阅 | **31** 次（≤ 8 品种 × 4） | 只对真正需要的品种取深度 ✅ |
| 负载前后磁盘增长 | 行情 24 KiB、意图 32 KiB、订单 40 KiB、账本 0 | 负载量级下的增长可观测 ✅ |

### 4.2 时钟与调度（`docs/evidence/2026-11-17-ac26-clock.json`）

| 指标 | 实测 |
|---|---|
| 注入跳变 | 前跳 3600s（实测 3595.0s，方向 forward） |
| 触发结果 | 登记**锁定**暂停 `CLOCK_JUMP`，状态机 `PAUSED_OPEN`，`allows_new_exposure=false` |
| 新意图 | 被拒（"暂停新增风险或合约不可交易"），已批准意图 0；被拒意图仍留痕（1 条） |
| 偏差阈值 | 5.0s |

### 4.3 回归

`pytest tests` → **1386 passed in 103.14s**（12 条 SQLAlchemy 连接生命周期警告，与本轮无关）；`ruff check src/bybitbot tests` → All checks passed。
本轮改动后曾出现 3 项失败，已定位并修复：容量限速最初把超出的计划直接跳过，导致沙盒验收的两个同秒策略只落地一个订单；修复为"延后"后，沙盒验收改为等待限速窗口（见第 6 节）。

## 5. 未覆盖与限制

| 项 | 说明 |
|---|---|
| 参考环境 | NFR-02 的 4 vCPU/8GB 是**参考环境**；本轮在本机（macOS arm64）实测。基线余量很大，但不等同于参考环境实测 |
| 真实行情 | 全部使用合成行情与合成深度；不含交易所网络等待，因此 P95 只反映本地校验与数据库事务 |
| 长期保留 | 1 年保留是配置与"只删未引用原始行情"的实现约束，没有做 1 年实跑验证 |
| 生产时钟 | AC-26 的跳变由注入的假时钟产生；真实 NTP 守护进程与生产主机实际跳变未实测 |
| 实盘 | 本轮不涉及任何实盘凭据、真实账户或交易所写请求 |

## 6. 本轮的行为变化（运营须知）

| 变化 | 说明 |
|---|---|
| 限速从"跳过"改为"延后" | 撞上每秒 1 个意图上限的计划**不再被丢弃**，而是回到 `PLANNED` 等下一个调度周期再提交（`Scheduler.defer`）。丢弃会让同一秒到期的另一个策略当天静默不交易；延后既守住上限也保住计划。延后有界：错过 60s 宽限期后按"错过即跳过"处理，不集中补买 |
| 沙盒验收会等待限速窗口 | 沙盒的两个策略在同一秒到期，验收改为驱动系统直到计划全部落地（最多 10 轮，每轮间隔 1.05s），而不是假定一次 `_schedule()` 就全部提交 |
| 时钟恢复不再自动解除 | 时钟偏差恢复后必须同时满足身份、对账、基线、执行权、估值、无负债、无未知订单，才会解除暂停；技术性重新同步与重启都不能解除锁定的 `CLOCK_JUMP` |
| 新增每日保留清理 | 每日执行一次，只删未引用的原始行情快照；结果写入运行状态 |

## 7. 后续建议

1. 把本轮两个证据文件纳入发布流水线，每次交付自动重算 P95 与延迟分布；
2. 在 NAS 参考环境上重跑容量用例，补 4 vCPU/8GB 的实测数字；
3. M3 Demo/Testnet 联调时，把时钟与磁盘故障注入到真实环境再验一次；
4. AC-24 仍待真实只读/可写凭据，不受本轮影响；
5. 限速延后意味着同一秒到期的多个策略会分摊到不同调度周期，接入真实行情前应复核策略日历时刻是否扎堆。
