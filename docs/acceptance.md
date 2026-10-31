# Bybit 现货/期权自动交易系统 —— 当前验收矩阵（AC-01 ~ AC-28）

生成日期：2026-10-31
依据：`docs/requirements.md` v0.1 §7 验收用例、§4 功能需求编号、§6 非功能需求、§8 实施阶段
代码基线：`/Users/nicholaswang/Desktop/code/bybit`（`src/bybitbot`、`tests/`、`deploy/`、`alembic/`）
测试基线：`pytest --collect-only -q` → **191 项**本地隔离测试；全部为本地隔离 / 模拟环境用例。

---

## 0. 文档口径与免责声明

### 0.1 状态定义

| 状态 | 含义 |
|---|---|
| **已验证** | 需求语义有对应自动化测试，断言直接覆盖该语义，且在本地隔离环境可重复执行并通过。 |
| **部分验证** | 需求语义有实现，且有部分自动化断言；但存在未覆盖的场景、未断言的实现分支，或仅有接口契约级（MockTransport）证据而无端到端证据。 |
| **待验证** | 需求语义缺少有效证据：既无自动化测试，或仅有代码而无可执行断言；或必须在真实 Bybit 账户 / 非实盘环境 / 长时间运行中才能取得证据。 |

### 0.2 关键声明（务必与结论一同引用）

1. **测试通过 ≠ 实盘通过 ≠ 真实账号通过。** 本仓库全部 191 项测试均在 `BYBITBOT_TESTING=1` 下运行，`tests/conftest.py::block_exchange_network` 会阻断一切外部 HTTP，交易所行为全部由 `httpx.MockTransport` 与 `PaperAdapter` 合成。因此所有证据仅覆盖「代码在合成环境下的行为」，**不构成 Bybit 真实接口、真实账户模式、真实期权链、真实成交/撤单/结算能力的证据**。
2. **未提供真实 Bybit API Key/secret**，`config/credentials*.json` 未装载，`runtime.option_units_verified` 仍为未验证态。真实 Demo/Testnet 能力（AC-24）与实盘能力均**未取得任何真实证据**。
3. **「实盘可用」相关门槛全部未开启**：`live_gates.live_allowed=false`、`acceptance_record_ref=null`、`manual_enable_ref=null`（`config/config.example.yaml:192-195`），`ops.alert_channels=[]`、`ops.risk_start_at=null`。`TradingService.start()` 在 `live` 下会直接拒绝启动（`src/bybitbot/service.py:217-222`）。
4. 本文档描述的是**当前代码 + 当前测试**的验收状态，不沿用任何历史结论（例如"全部 M3 完成"）。`docs/requirements.md` §8 末段"当前仅交付文档，没有创建账户、部署服务或执行任何交易"是需求撰写时点的历史描述，**不是当前事实**；当前事实见 §4。
5. 每条"剩余差距"均指**当前无法用现有证据判定该需求已满足**的部分，不等同于"实现必然错误"。

### 0.3 覆盖需求编号速查

本文引用的需求编号含义（详见 `docs/requirements.md` §4）：`ENV`环境接入 / `MKT`行情与品种 / `STR`策略与调度 / `RSK`独立风控 / `EXE`订单执行 / `SPT`现货 / `OPT`期权 / `LED`账本对账损益 / `OPS`管理告警运行状态 / `SEC`安全 / `NFR`非功能。

---

## 1. 结论摘要

### 1.1 总体统计

| 状态 | 数量 | 用例编号 |
|---|---|---|
| 已验证 | **0** | — |
| 部分验证 | **25** | AC-01 ~ AC-05、AC-06 ~ AC-21、AC-23、AC-25、AC-27、AC-28 |
| 待验证 | **3** | **AC-22**（容量/性能/保留）、**AC-24**（真实非实盘环境能力）、**AC-26**（时钟/调度健壮性，模块零测试） |

> 没有用例被判为"已验证"，原因统一为：每条验收用例都同时包含"可模拟的正常链路"与"必须靠真实环境/真实故障注入/长时间运行才能取证"的部分（见 `requirements.md` §7 执行级别说明）。这与需求本身的定义一致，不应被解读为"实现不合格"。

### 1.2 按里程碑对照

| 里程碑（requirements §8） | 当前状态 | 说明 |
|---|---|---|
| M0 开发基线 | 基本达成 | 需求、评审报告、配置模式、数据模型、测试计划齐备；`alembic` 三段迁移、`uv.lock`、`deploy/` 脚本、`.github/workflows/test.yml` 齐备 |
| M1 只读接入（AC-01~AC-03 只读部分） | **模拟层达成，实账户未达成** | 合约目录/钱包/权限解析有契约级测试；真实 UID、权限、账户模式未核对 |
| M2 模拟闭环（AC-04~AC-19 可模拟部分） | **大体达成，但关键分支无测试** | 核心不变量有测试；风控拒绝分支、时钟、估值口径、保留期几乎无测试 |
| M3 非实盘联调 | **未达成** | 无真实 Demo/Testnet 凭据与证据；适配器契约测试≠非实盘联调 |
| M4a 受限实盘 | **未开始** | 无实盘允许配置、无验收记录、无人工启用记录、无真实凭据 |
| M4b 正式无人值守 | **未开始** | — |

### 1.3 最关键的 9 个缺口（G1、G9 已于本轮关闭）

| # | 缺口 | 影响用例 | 说明 |
|---|---|---|---|
| G1 | ~~**`valuation.py` 零测试**~~ **已关闭** | AC-17, AC-25 | 新增 `tests/test_valuation_secrets.py`（27 项）：覆盖 `value_account` 的自有净现金+期权价值口径、负债不完整、缺汇率/缺标记价、合约乘数、空头不计值、日损益扣除资金流、回撤不为负、日界线跨时区 |
| G2 | **`clock.py` 关键符号零测试**（测试只导入 `now_utc`），且时钟跳变/偏差"暂停新开仓"无端到端断言 | AC-26 | NFR-06 的核心保护未经验证 |
| G3 | **风控拒绝分支大面积无测试**：`SIGNAL_FRESHNESS` / `PER_TRADE_LIMIT` / `PRICE_DEVIATION` / `SPREAD` / `FEE_BUFFER` / `MAX_OPEN_ORDERS` / `SPOT_EXPOSURE` / `PREMIUM_BUDGET` / `PREMIUM_CONCENTRATION_*` / `OPTION_EXPIRY` / `NO_OPEN_WINDOW` 在 `tests/` 中**一次都没有出现** | AC-06, AC-14, AC-15, AC-27 | 风控引擎实现了 20+ 检查码，测试只触及 6 个；RSK-01/03/05 的多数拒绝路径无证据 |
| G4 | **性能与容量无任何测试**（NFR-01/02/05）：无 P95 延迟测量、无容量上限测试、无磁盘不足暂停测试、无数据保留清理实现 | AC-22, AC-26 | `ops.retention_days=365` / `raw_market_data_retention_days=7` 只有配置项，全仓检索无任何 purge/prune 实现 |
| G5 | **崩溃恢复只覆盖 2/4 个崩溃点**：持久化前、持久化后发送前有测试；"发送后未保存响应"、"部分成交后"无进程级崩溃演练 | AC-09 | 全部是同进程内模拟，没有真实 `kill -9` 演练 |
| G6 | **策略配置版本变更链路无测试**：不可变版本递增、旧订单保留原决策版本、收紧限额后重评未完成挂单、停用策略撤单，均无断言 | AC-06 | `service._apply_config` 有实现但测试只间接用到 `strategy_version` |
| G7 | **非实盘环境能力完全未验证**：期权权限、`reduceOnly`、持仓模式、Greeks 单位、到期结算结构 | AC-24 | 仅有 MockTransport 契约测试 |
| G8 | **告警与外部监控的端到端链路未验证**：真实通知渠道未配置（`alert_channels: []`）；Mac 端监控已投运但**告警送达路径未做真实触发演练** | AC-20 | 投递逻辑只在本地 MockTransport 下测过 |
| G9 | ~~**`settings/secrets.py` 零测试**~~ **已关闭** | AC-21 | 同批新增：权限过宽（0644/0640）拒绝、文件缺失、字段缺失、非法 JSON、`repr`/`masked_summary` 不泄露明文、`redact()` 脱敏。

---

## 2. 验收矩阵总表

| 用例 | 覆盖需求 | 状态 | 主要证据（测试名 / 代码位置） | 剩余差距（摘要） |
|---|---|---|---|---|
| AC-01 | ENV-01/02/03 | 部分验证 | `test_endpoint_mismatch_rejected`、`test_public_reads_have_no_auth_and_paper_discards_credentials`、`test_demo_routes_public_and_private_to_separate_hosts`、`test_key_identity_fields_fail_closed`、`test_live_test_guard_blocks_even_explicit_enabled_mock_write`、`test_paper_guard_blocks_remote_writes`、`test_only_fixed_ip_key_binding_counts_as_bound` | 配置层校验（独立 DSN、live 必填 UID、`LiveGateConfig` 三件套、实盘启动门槛）**无任何 `ConfigError` 断言测试**；真实 UID/权限/账户模式未比对 |
| AC-02 | MKT-01、SPT-01 | 部分验证 | `test_spot_spec_has_no_pagination_and_uses_limit_precision`、`test_option_specs_all_bases_paginated_and_strike_from_symbol`、`test_repeated_cursor_fails_instead_of_partial_snapshot`、`test_spot_no_leverage_and_signed_json_bytes`、`test_unknown_order_status_and_impossible_quantity_fail_closed` | "规格变更后新订单采用更新规则或被拒绝"（`service.py:1280-1294`）无测试；退市/非 Trading 状态拒绝开仓无测试；未验证不调用借贷接口 |
| AC-03 | MKT-02/03、RSK-03 | 部分验证 | `test_ws_invalid_delta_requires_fresh_snapshot`、`test_ws_no_snapshot_and_reconnect_invalidate_delta`、`test_ws_crossed_book_invalidates_without_applying_partial_changes`、`test_ws_invalid_snapshot_cannot_restore_trust`、`test_ws_snapshot_zero_levels_filtered_and_repeated_snapshot_allowed`、`test_ws_seq_and_update_may_jump_without_false_gap`、`test_private_disconnect_invalidates_before_backoff`、`test_risk_uses_cash_and_two_sided_per_base_delta`、`test_missing_option_greeks_never_zero_delta` | WS 层 fail-closed 已验证；**服务层"行情失效→暂停相关开仓→重建后恢复"整条链路无端到端测试**；决策快照引用未落不可变快照表并被断言 |
| AC-04 | STR-01/02 | 部分验证 | `test_schedule_100_duplicates_claim_once_and_skip_missed`、`test_enabled_template_schedule_fills_once_across_restart`、`test_global_physical_ids` | 时区跨日去重未测；过期信号被拒无直接断言；意图字段完整性（STR-01 十一项）无字段级断言 |
| AC-05 | STR-03、OPT-03 | 部分验证 | `test_enabled_template_schedule_fills_once_across_restart[option]`、`strategy/templates.py:152` 固定排序与 `rationale.sort_rule` | "固定行情回放得同一合约同一理由"无重复性断言；"无符合条件合约时跳过"无直接测试；`profit_threshold` / `before_expiry` 退出规则**无任何测试** |
| AC-06 | STR-04、RSK-01 | 部分验证 | `test_all_commands_only_enqueue_once`、`test_commands_reject_invalid_scope_and_confirmation`、`test_csv_trace_joins_and_escapes_formulas`（断言 `risk_config_version`） | 配置不可变版本链路无测试；旧订单保留原版本无测试；收紧限额重评挂单/停用策略撤单无测试；"未完整配置不能实盘启用"无测试 |
| AC-07 | RSK-02、SPT-02 | 部分验证 | `test_cash_reservation_competition`、`test_cash_freeze_overlap_requires_order_evidence`、`test_inventory_close_competition_and_strategy_ownership`、`test_inventory_freeze_overlap_requires_snapshot_evidence`、`test_costs_follow_fee_units_and_strategy` | 账户级现金竞争已覆盖，但两个并发预留来自**同一 `strategy_id`** 的两个意图，不是"两个不同策略共享账户级预算并各自受策略子额度约束"；零碎余额未实现；"运行中新增负债→暂停+告警"端到端无测试 |
| AC-08 | EXE-01/02 | 部分验证 | `test_unknown_never_retries_or_releases_missing_query`、`test_duplicate_id_is_not_rejection_evidence`、`test_ambiguous_writes_sent_once`、`test_explicit_write_rejection_is_not_unknown`、`test_ack_identity_mismatch_is_uncertain`、`test_recovery_orphan_reservation_proves_unsent_and_cannot_revive`、`test_prepared_unsent_recovery_releases_cash_units`、`test_cancel_ack_is_only_acceptance` | `resolve_unknown` **成功路径无正例**；"活动订单消除未知状态"无正例；"人工点击恢复不能释放预留"无测试 |
| AC-09 | EXE-01/02、LED-02 | 部分验证 | `test_recovery_orphan_reservation_proves_unsent_and_cannot_revive`、`test_prepared_unsent_recovery_releases_cash_units`、`test_reconciliation_retains_offline_fill_and_watermark`、`test_start_baseline_takeover_trade_restart` | 4 个崩溃点只覆盖 2 个（缺"发送后未保存响应""部分成交后"）；**无真实进程 kill 演练** |
| AC-10 | EXE-03、LED-01 | 部分验证 | `test_terminal_before_fill_preserves_reservation_and_no_double_count`、`test_late_fill_after_cancel_reclassifies_release_and_upgrades_full`、`test_terminal_and_fill_monotonic_state`、`test_private_events_dedupe_category_and_do_not_lose_other_category`、`test_private_invalid_execution_does_not_enter_dedupe_cache` | "先成交后撤单到达"的乱序顺序未单独覆盖；更正事件（`correction_of_id`）无测试；去重域实现为 `exchange_exec_id` 全局键，与需求"账户/环境/产品+成交编号"分域定义不完全一致 |
| AC-11 | EXE-04 | 部分验证 | `test_read_retries_bounded_and_limit_headers_recorded`、`test_long_server_cooldown_fails_without_retry_storm`、`test_transport_read_failure_retries_only_configured_attempts`、`test_read_permission_failure_is_not_retried_and_raw_message_redacted`、`test_ambiguous_writes_sent_once`、`test_exit_rebuild_preserves_deadline_quantity_and_floor`、`test_cancel_ack_without_terminal_preserves_held` | 本地 TTL 到期触发撤单无测试；"停机错过 TTL"无测试；**子订单累计支出不突破 `max_intent_total_spend` 无断言**（只断言 `child.qty <= exit_intent.qty`）；撤单查询时限无超时测试 |
| AC-12 | EXE-05、RSK-03 | 部分验证 | `test_lease_single_holder_and_no_auto_takeover`、`test_remote_adapter_cannot_use_paper_label_to_bypass_lease`、`test_start_baseline_takeover_trade_restart` | "同时启动两个实例"为同进程两个 lease 对象，**非双进程/双容器实测**；"断开数据库连接"用 `pg_advisory_unlock_all()` 模拟，非真实断连；"失权时在途请求按待核实处理"无测试 |
| AC-13 | OPT-01/02 | 部分验证 | `test_option_sale_requires_reduce_only_and_one_way_position`、`test_option_close_credits_cash_and_cannot_short`、`test_inventory_close_competition_and_strategy_ownership`、`test_external_fill_is_recorded_but_never_owned`、`test_missing_position_locks_inventory_and_keeps_cost` | "两个平仓信号竞争同一多头"无真实并发授权测试；部分成交+未知卖单+撤单竞态未组合；"外部改仓时停止自动卖出"链路无测试；跨策略减仓分摊无测试 |
| AC-14 | RSK-05、OPT-03 | 部分验证 | `test_opposite_orders_cannot_offset`、`test_unquantified_delta_rejected`、`test_risk_uses_cash_and_two_sided_per_base_delta`、`test_missing_option_greeks_never_zero_delta` | 大负 Delta、已确认外部库存、现货新单改变账户 Delta 无独立场景；**权利金/到期集中度/价格边界超限拒绝全部无测试**；Greeks 单位映射未用样例验证；Gamma/Vega/Theta 有记录字段但无验收断言 |
| AC-15 | OPT-04、LED-01 | 部分验证 | `test_paper_settlement_checkpoint_and_ledger_replay[90-0]`、`test_paper_settlement_checkpoint_and_ledger_replay[120-2]`、`test_settlement_evidence_idempotency_and_cost_release`、`test_invalid_book_and_non_touch_do_not_fill` | 禁开仓窗口拒绝买入 / TTL 不晚于窗口起点 / 进入窗口撤单 / 撤单未成功告警展示余量，**四者全部无测试**；到期结算用合成 fixture，非真实交割结构 |
| AC-16 | LED-02/03、OPT-02 | 部分验证 | `test_external_fill_is_recorded_but_never_owned`、`test_external_cash_change_cannot_be_clean_recon`、`test_missing_position_locks_inventory_and_keeps_cost`、`test_reconciliation_retains_offline_fill_and_watermark` | "不自动撤销人工单"无测试；"人工确认后审计化重建基线"整链无端到端测试；导入已有库存的归属与成本来源无测试 |
| AC-17 | LED-04、RSK-03 | 部分验证 | `test_start_baseline_takeover_trade_restart`、`test_external_cash_change_cannot_be_clean_recon`、`test_loss_pause_does_not_block_trusted_reduce`、`test_equity_is_own_cash_plus_option_value_without_double_counting`、`test_day_pnl_subtracts_external_flow`、`test_adjusted_equity_removes_cumulative_flow`、`test_drawdown_is_peak_minus_current_and_never_negative` | 估值公式已补单元测试（本轮）；重启不重置回撤高点无跨重启断言；"失效估值不解除暂停"与 `VALUATION_UNAVAILABLE` 属技术暂停（可自动解）的语义**存在冲突需评审**；跨币费用、标记价与买价差异、无重复资产估值均无测试 |
| AC-18 | RSK-04、OPS-02/03 | 部分验证 | `test_loss_pause_does_not_block_trusted_reduce`、`test_all_commands_only_enqueue_once`、`test_commands_reject_invalid_scope_and_confirmation`、`test_completed_command_preserves_actual_results` | "触发亏损阈值→停止开仓并撤销 bot 买单"端到端无测试；DB 不可写不新发单无测试；重启/换日不解除锁定无跨重启断言；RESUME 前置对账校验无测试 |
| AC-19 | OPS-01/02 | 部分验证 | `test_status_is_environment_scoped_and_allowlisted`、`test_csv_trace_joins_and_escapes_formulas`、`test_console_requires_session_and_serves_local_assets`、`test_all_commands_only_enqueue_once` | 状态页字段齐全（含 `risk_rejects`、`orders`、`alerts`、`reconciliations.differences`），但**测试未断言币种/未知订单/风控拒绝原因的具体展示**；金额带币种的覆盖面未逐项断言 |
| AC-20 | OPS-04/05、NFR-03 | 部分验证 | `test_delivery_retry_dedupe_and_resolution`、`test_independent_monitor_three_misses_one_alert_and_recovery`、`test_offsite_backup_staleness_is_independent_of_health`、`test_backup_age_uses_dump_time_instead_of_copy_time`、`test_installer_keeps_jobs_outside_protected_desktop` | 真实通知渠道未配置（`alert_channels: []`），仅 MockTransport；"关键告警超时未送达→暂停新增风险"（`service.py:1917`）无测试；"告警含剩余仓位"无断言；8 类告警触发源未逐项测 |
| AC-21 | SEC-01/02/03 | 部分验证 | `test_console_requires_session_and_serves_local_assets`、`test_login_cookie_and_error_responses_hide_password`、`test_sessions_expire_absolutely_and_logout_revokes`、`test_web_restart_invalidates_old_session`、`test_login_requires_exact_origin`、`test_login_rate_limit_is_bounded_and_recovers`、`test_control_requires_origin_and_csrf`、`test_admin_file_rejects_broad_or_wrong_mode`、`test_admin_file_rejects_symlink_and_malformed_hash`、`test_admin_generation_permissions_no_overwrite`、`test_read_permission_failure_is_not_retried_and_raw_message_redacted`、`test_status_is_environment_scoped_and_allowlisted`、`test_csv_trace_joins_and_escapes_formulas`、`test_live_test_guard_blocks_even_explicit_enabled_mock_write` | 无自动化"密钥泄漏扫描"（代码/日志/镜像/导出）；"数据库不向公网开放"无检查；固定出口 IP 仅测 key info 解析，未测部署出口；**凭据轮换流程无测试**；"关闭提现权限"只解析未断言拒绝 |
| AC-22 | NFR-01/02/05 | **待验证** | 无对应测试 | 无延迟分布/CPU/内存/订阅量/磁盘增长记录；无容量上限测试；磁盘不足暂停无测试；**数据保留清理未实现**（仅配置项，全仓无 purge 实现）；"不静默丢弃账务记录"无测试 |
| AC-23 | OPS-05、NFR-04、LED-02 | 部分验证 | `test_backup_age_uses_dump_time_instead_of_copy_time`、`test_offsite_backup_staleness_is_independent_of_health`、`deploy/backup.sh`（`pg_dump -Fc` + `gpg` 加密） | 无自动化恢复演练测试；RTO ≤ 2h / RPO ≤ 24h 无测量记录；"未能确认历史完整性时不恢复交易资格"无测试；恢复交易资格未验证（人工验证见 §4） |
| AC-24 | ENV-02、OPT-01/03/04 | **待验证** | 仅契约级：`test_option_sale_requires_reduce_only_and_one_way_position`、`test_option_specs_all_bases_paginated_and_strike_from_symbol`、`test_option_bulk_ticker_mark_iv_and_depth_cap`、`test_option_empty_registry_is_not_complete_history`、`test_wallet_nested_net_cash_and_uid` | 无真实凭据；期权权限、减仓参数、成交/撤单、到期结算能力**全部未实测**；账户模式（逐仓/组合）、结算币种、Greeks 单位未验证；Demo 是否支持全部接口未验证 |
| AC-25 | EXE-03、SPT-02、OPT-02、LED-04 | 部分验证 | `test_costs_follow_fee_units_and_strategy`、`test_inventory_close_competition_and_strategy_ownership`、`test_settlement_evidence_idempotency_and_cost_release`、`test_paper_settlement_checkpoint_and_ledger_replay` | "同合约两策略分批买入再部分平仓"无测试（仅单策略）；跨币种费用组合无测试；"权利金支付→持仓估值→平仓/到期现金"全过程无端到端断言；"无重复资产估值"无测试 |
| AC-26 | NFR-06、STR-02 | **待验证** | 实现见 `clock.py`（`monotonic`/`Deadline`/`backoff_delay`/`ClockSkewMonitor`/`local_day_start_utc`）、`service._clock_and_disk`；`test_schedule_100_duplicates_claim_once_and_skip_missed` | **`clock.py` 关键符号零测试**（测试只导入 `now_utc`）；时钟前跳/回拨/超偏差"暂停新开仓"无端到端断言；"调度不重复补做"覆盖不全；退避时长不受墙钟跳变破坏无测试 |
| AC-27 | RSK-02、OPT-03、ENV-02 | 部分验证 | `test_wallet_debt_not_double_counted_or_spendable`、`test_portfolio_unknown_holds_have_zero_spendable_cash`、`test_cash_freeze_overlap_requires_order_evidence`、`test_cash_reservation_competition` | "抵押品充足但结算币现金不足→期权买单被拒"无专项测试；"运行中注入新增负债→暂停并告警"端到端无测试；"不自动借贷或划转"无专项断言 |
| AC-28 | STR-05 | 部分验证 | `test_next_book_depth_shared_cancel_and_checkpoint`、`test_invalid_book_and_non_touch_do_not_fill`、`test_unknown_registered_and_survives_json_restore`、`test_paper_guard_blocks_remote_writes` | "同一事件序列两次运行结果相同"无确定性重放断言；"报告标记模拟及模型限制"无断言；"故障实验报告与策略表现报告分别标识"未实现；故障注入仅 `unknown_result` 一种被使用 |

---

## 3. 逐项说明

### AC-01 · ENV-01 / ENV-02 / ENV-03 —— 部分验证

**已取证**
- 端点与环境强绑定：`test_endpoint_mismatch_rejected`（4 个参数）拒绝 `http://`、子域后缀欺骗（`api-testnet.bybit.com.evil.test`）、带 userinfo 的 URL。
- Demo 公私分离：`test_demo_routes_public_and_private_to_separate_hosts` 断言公共读打到 `api.bybit.com`、私有读打到 `api-demo.bybit.com`，且公共请求不带 `X-BAPI-API-KEY`。
- Paper 不装载凭据：`test_public_reads_have_no_auth_and_paper_discards_credentials` 断言 `api_key is None and api_secret is None`；`test_paper_guard_blocks_remote_writes` 断言 paper 下写请求被拒。
- 自动化测试阻断实盘写：`test_live_test_guard_blocks_even_explicit_enabled_mock_write` —— 即使显式 `allow_trade_writes=True` 且 `BYBITBOT_TESTING=1`，实盘写仍在 transport 层之前被拒（`adapters/bybit_rest.py:215`）。
- 身份字段 fail-closed：`test_key_identity_fields_fail_closed`（5 个参数）覆盖 `readOnly`/`userID`/`ips`/`permissions` 缺失或类型错误。
- IP 绑定语义：`test_only_fixed_ip_key_binding_counts_as_bound`、`test_wildcard_does_not_become_bound_because_real_ip_also_present` —— `*` 通配不算已绑定。
- 负债不计入可用现金：`test_wallet_debt_not_double_counted_or_spendable`（2 参数）、`test_portfolio_unknown_holds_have_zero_spendable_cash`。

**剩余差距**
1. **配置层校验完全无测试**。`settings/loader.py::_cross_validate` 实现了一批硬校验——各环境必须独立 `db_dsn`、live 必须 `expected_uid`、资金/损失/总支出币种必须等于估值币种、策略数 ≤ 5、live 最大时钟偏差 < 5 秒、禁开仓窗口 TTL < 常规订单 TTL——但 `tests/` 中**没有任何一处 `ConfigError` 断言**，`load_config` 只被用于加载合法样例。ENV-01/ENV-03 的"隔离"和"门槛"在配置层是不可绕过的，这条不变量目前没有回归保护。
2. **`LiveGateConfig` 三件套无测试**。`live_allowed=true` 必须同时有 `acceptance_record_ref` 与 `manual_enable_ref`（`settings/models.py:633-636`），`TradingService.start()` 在 live 下还会额外要求 `alert_channels` 非空且 `risk_start_at` 非空（`service.py:217-222`）——**全部无测试**。
3. **真实账户身份未比对**。ENV-02 要求"启动时校验预期 UID/子账户、API 权限、账户模式"。代码路径存在（`get_key_info` → `userID`、`permissions`、`ips`；`get_account` → `account_mode`），但从未拿真实 Key 跑过一次，`config/account.expected_uid` 在示例中为注释占位。
4. **不自动修改账户设置**无断言：代码只读不写，无回归保护。

---

### AC-02 · MKT-01 / SPT-01 —— 部分验证

**已取证**
- 现货规格解析：`test_spot_spec_has_no_pagination_and_uses_limit_precision` 断言 `qty_step=0.000001`（来自 `basePrecision`）、`max_order_qty=71.73`（来自 `maxLimitOrderQty`）、`min_order_amt=5`，且请求参数只有 `category=spot`（现货目录不分页）。
- 期权目录分页：`test_option_specs_all_bases_paginated_and_strike_from_symbol` 断言 `baseCoin=All` 起查并跟 `nextPageCursor`；行权价从 symbol 解析（`ETH-30OCT26-3000-C-USDT` → 3000）；`optionsType`/`settleCoin` 正确。
- 分页安全：`test_repeated_cursor_fails_instead_of_partial_snapshot` —— 重复游标直接报错，不返回半份快照（对应 MKT-01"字段缺失或解析失败时拒绝相关交易"）。
- 现货不借贷：`test_spot_no_leverage_and_signed_json_bytes` 断言 `isLeverage == 0` 且签名覆盖精确编码字节。
- 未知字段 fail-closed：`test_unknown_order_status_and_impossible_quantity_fail_closed`。

**剩余差距**
1. **"新订单采用更新规则或被拒绝"无测试**。`service.py:1280-1294` 已经实现了全部校验（`qty % qty_step != 0`、`price % tick_size != 0`、`min_order_qty`、`max_order_qty`、`min_order_amt`、`is_tradable`），但**没有任何测试构造"修改步长后旧数量不再合法"的场景**。MKT-01 的核心不变式（规格变化 → 不扩大预算 → 拒绝）目前只有人工阅读保证。
2. **退市/非 Trading 状态合约**：`market.py::_upsert` 会更新 `status`，`templates.py` 与 `service.py` 会检查 `is_tradable`，但"白名单外的品种不可开仓""交割中的合约不可开仓"无专项测试。
3. **"不调用借贷接口"未被证明**。`test_spot_no_leverage_and_signed_json_bytes` 只证明请求体里 `isLeverage=0`；"从不下发任何借贷/划转请求"需要更宽的断言（例如对全部出站路径做白名单校验）。
4. 真实接口字段语义（`maxLimitOrderQty` vs `maxOrderQty` 的实际含义）仅由 MockTransport 契约确定，未对真实响应验证。

---

### AC-03 · MKT-02 / MKT-03 / RSK-03 —— 部分验证

**已取证**
- 断档与重连：`test_ws_invalid_delta_requires_fresh_snapshot`（5 参数）、`test_ws_no_snapshot_and_reconnect_invalidate_delta`（重连后必须重新快照）。
- 交叉报价：`test_ws_crossed_book_invalidates_without_applying_partial_changes` —— 拒绝增量且**不落地部分变更**。
- 无效快照不可恢复信任：`test_ws_invalid_snapshot_cannot_restore_trust`（4 参数）。
- 零档位过滤与重复快照：`test_ws_snapshot_zero_levels_filtered_and_repeated_snapshot_allowed`。
- 序列号跳变不误报：`test_ws_seq_and_update_may_jump_without_false_gap`。
- 断流即失效：`test_private_disconnect_invalidates_before_backoff` 断言退避开始前 `connected=False` 且 `needs_reconciliation=True`。
- 过期报价拒绝开仓：`test_risk_uses_cash_and_two_sided_per_base_delta` 尾部用 1 小时前的 `source_ts` 断言拒绝。
- Greeks 缺失不当作 0：`test_missing_option_greeks_never_zero_delta` → `DELTA_UNQUANTIFIED`。

**剩余差距**
1. **服务层闭环未测**。WS 层"拒绝坏数据"已验证，但"行情失效 → 写入 `MARKET_DATA_STALE` / `MARKET_DATA_GAP` 暂停 → 只暂停相关品种 → 重新同步后恢复"的整条链路无端到端测试（`PauseReason.MARKET_DATA_STALE` / `MARKET_DATA_GAP` 在 `tests/` 中一次都没出现）。
2. **"只暂停相关品种，但不能沿用其旧估值证明账户安全"**（RSK-03）无测试。`PauseManager.is_paused` 实现了品种级暂停语义，但账户级估值路径不会因为品种暂停而失效——这一点缺少断言来固定预期。
3. **"记录策略决策使用的数据快照引用"**：`IntentDraft.rationale` 里只有 `ask/budget/fee_rate/qty_step` 几个字符串，**没有不可变的行情快照引用**（`market.py:280 persist_snapshot` 存在但未与策略决策关联并被断言）。
4. `market.max_quote_age_seconds` 在服务层的实际使用无独立断言。

---

### AC-04 · STR-01 / STR-02 —— 部分验证

**已取证**
- `test_schedule_100_duplicates_claim_once_and_skip_missed`：100 个并发 `plan()` 只产生 1 条 `ScheduleRun`；5 个并发 `claim_due()` 只领取 1 条；`finish(DONE)` 后再 `finish(FAILED)` 不覆盖终态；冷却期 `in_cooldown` 为真；过期 1 小时的计划被标记 `SKIPPED` 且 `claim_due()` 不返回它 —— 直接对应"至多一个有效业务意图""已过期任务不集中补买""冷却时间持久化"。
- `test_enabled_template_schedule_fills_once_across_restart`（spot / option 两个参数）：重启后不重复成交。
- `test_global_physical_ids`：1000 个 `orderLinkId` 互不相同且长度 ≤ 36（交易所限制）。

**剩余差距**
1. **"包含重启与时区跨日"只覆盖了重启**。跨日（Asia/Shanghai 00:00 日界线切换）对去重键的影响无测试；`local_day_start_utc` 参与日界线的行为只在 `valuation.py` 侧被间接使用。
2. **STR-01 的字段完整性没有字段级断言**。`TradeIntent` 模型确实含"策略与配置版本、唯一信号编号、账户/环境、产品与合约、开平仓目的、方向、数量及单位、价格边界、有效期、退出规则引用"，但没有测试逐项验证这些字段被正确填充。
3. **"过期信号被拒绝"无直接测试**。`RiskEngine._check_signal_freshness` 存在（检查码 `SIGNAL_FRESHNESS`），但该检查码在 `tests/` 中一次都没出现。
4. **去重键的数据库唯一约束未被直接断言**。行为层被测到，但 `ScheduleRun` 上的唯一索引本身无测试保护。

---

### AC-05 · STR-03 / OPT-03 —— 部分验证

**已取证**
- `test_enabled_template_schedule_fills_once_across_restart[option]`：启用期权模板后按 `strike_range_pct` / `delta_range` / `max_premium` / `min_open_interest` 筛选，选中 `BTC-10NOV26-100-C`，并产生 1 个 `OPTION_BUY_OPEN` 意图 + 1 个订单 + 1 个成交。
- 排序确定性：`strategy/templates.py:152` `candidates.sort(key=lambda c: (c["premium"], c["expiry"], c["symbol"]))`，并把 `sort_rule="ask asc, expiry asc, symbol asc"` 与 `candidates` 数量写入 `rationale`。

**剩余差距**
1. **"固定行情回放得到同一合约及同一理由"没有重复性断言**。没有"同一输入连续两次 `evaluate()` 结果完全相同"的测试，也没有把 `rationale` 全文做快照断言。
2. **"无符合条件合约时跳过"没有直接测试**（返回空列表的分支未被断言）。
3. **退出规则只测了一种**。`exit_rules()` 支持 `hold_period` / `profit_threshold` / `before_expiry` 三种；测试只用了 `hold_period`。止盈触发、到期前退出**无任何测试**，这与 AC-05"止盈/止损/到期前退出按指定报价和费用口径触发"直接冲突。
4. **"候选排序和并列决胜规则固定并记录"只有代码，无断言**。

---

### AC-06 · STR-04 / RSK-01 —— 部分验证

**已取证**
- 控制命令幂等与范围校验：`test_all_commands_only_enqueue_once`（7 种命令 × 5 次并发 → 仅 1 条 `ControlCommand`、0 个 `TradeIntent`/`Order`/`RiskPause`）、`test_commands_reject_invalid_scope_and_confirmation`（6 组非法输入 → 422）。
- 风险决策落库与可追溯：`test_csv_trace_joins_and_escapes_formulas` 断言 CSV 行含 `risk_config_version=synthetic-v1`。
- 配置版本递增：`_apply_config` 在 `params` 变化时创建新 `StrategyConfigVersion`（`service.py:406-434`），被 `test_enabled_template_schedule_fills_once_across_restart` 间接使用（`strategy_version`）。

**剩余差距**
1. **STR-04 的四条子要求全部没有测试**：
   - "配置变更生成不可变版本，校验后原子生效" —— 无断言（旧版本是否保留、新意图是否用新版本）。
   - "正在处理的订单保留原决策版本" —— 无断言。
   - "收紧限额时重新评估未完成开仓单和预留，超限则暂停新增风险并撤销相关 bot 开仓单" —— 无断言。
   - "策略停用停止新开仓并撤销该策略开仓挂单，已有仓位退出管理保持运行" —— `_cancel_expired`（`service.py:1583-1600`）确有 `not strategy.enabled` 分支，无测试。
2. **RSK-01 的多数检查码无测试**（见 G3）。`RiskEngine._evaluate_open` 产出 20+ 个 `RiskCheckResult`，测试只触及 `IDENTITY_TRUSTED` / `VALUATION_TRUSTED` / `EXECUTION_AUTHORITY` / `UNKNOWN_ORDERS` / `ACCOUNT_PAUSED` / `DELTA_BOUNDS`。
3. **"缺少必填参数时拒绝实盘启用"无测试**（与 AC-01 缺口 2 重叠）。
4. **"撤单前成交正常入账"** 在执行层由 `test_late_fill_after_cancel_reclassifies_release_and_upgrades_full` 覆盖，但**在"策略停用撤单"这条具体业务路径上没有端到端断言**。

---

### AC-07 · RSK-02 / SPT-02 —— 部分验证

**已取证**
- `test_cash_reservation_competition`：两个并发 `reserve_cash` 请求同一 100 USDT 中的 60，只有一个成功（`ReservationError` 吞掉另一个）。
- `test_cash_freeze_overlap_requires_order_evidence`：交易所 `locked=40` 但快照未体现该冻结时 `usable=20`；快照 `reflected_order_link_ids` 命中该挂单时 `usable=60`（不重复扣除交易所冻结额与本地预留）；完全无法证明自有现金时抛 `ReservationError("缺少自有现金")`。
- `test_inventory_close_competition_and_strategy_ownership`：库存预留并发只 1 个成功；`closeable_qty` 扣除未完成预留后返回 0.3。
- `test_inventory_freeze_overlap_requires_snapshot_evidence`：库存冻结需快照证据，命中才加回。
- `test_costs_follow_fee_units_and_strategy`：基础币手续费抵扣（2 BTC − 0.1 = 1.9），均价仍为 10；卖出时按比例结转成本并计当次费用；不同策略成本不混合。

**剩余差距**
1. **"两个策略同时申请同一余额"未真正测到**。测试中两个并发请求用的是**同一个 `intent_id`**（`test_cash_reservation_competition` 里 `first`/`second` 两个 intent，但 `reserve()` 收到的 `identifier` 不同 —— 这一点是成立的），但账户级共享预算与策略级子额度（`STRATEGY_EXPOSURE_LIMIT` / `ACCOUNT_EXPOSURE_LIMIT`）的**同时扣减**没有测试。
2. **"持续检查负债，新增负债触发账户暂停和告警"端到端无测试**。`ACCOUNT_DEBT` 检查码存在但 `tests/` 中未出现。
3. **"零碎余额"（SPT-02 明确要求"小于最小下单要求的余额标记为零碎余额，不为清零而加仓"）没有实现也没有测试** —— 检索全仓没有"零碎/dust/remainder"相关逻辑。
4. **"不得漏算尚未反映在交易所快照中的预留"** 只有 `test_cash_freeze_overlap_requires_order_evidence` 的镜像情形被覆盖，反向情形（本地预留已占但交易所快照未体现）只在 `test_cash_reservation_competition` 间接体现。

---

### AC-08 · EXE-01 / EXE-02 —— 部分验证（证据较充分）

**已取证**
- `test_unknown_never_retries_or_releases_missing_query`：超时 → `is_unknown`；**第二次 `submit` 不会再发**（`adapter.calls == 1`）；`resolve_unknown` 在查不到结果时 `not resolved`；预留 `consumed==0 and released==0`；写入 `UNKNOWN_ORDER` 暂停。
- `test_duplicate_id_is_not_rejection_evidence`（3 参数）：`110072`、`170141`（重复单号）**不是**拒绝证据 → 仍为 unknown 且不释放预留；`110004` 才是明确拒绝 → 释放预留。
- `test_ambiguous_writes_sent_once`（5 参数：timeout / bad-json / duplicate / unknown-code / http500）：无论哪种歧义，**写请求只发一次**。
- `test_explicit_write_rejection_is_not_unknown`：明确拒绝抛 `OrderRejected` 并带 `ret_code`。
- `test_ack_identity_mismatch_is_uncertain`：回执 `orderId`/`orderLinkId` 不符 → `OrderUncertain`（下单与撤单都如此）。
- `test_recovery_orphan_reservation_proves_unsent_and_cannot_revive`：恢复流程能**证明未发送**并释放预留、写 1 条账本事件；该订单**不得复活**（再 `submit` 抛 `ExecutionAuthorityError("恢复流程终止")`）；`adapter.calls == 0`。
- `test_prepared_unsent_recovery_releases_cash_units`：`PREPARED` + `sync_state=NONE` 订单被证明未发送 → 释放全部 11 USDT、`sync_state=SYNC_CONFIRMED`。
- `test_cancel_ack_is_only_acceptance`：撤单回执只是受理（`accepted`，无 `terminal_confirmed`）。

**剩余差距**
1. **`resolve_unknown` 的成功路径没有正例**。AC-08 明确要求"查到结果后合并同一订单"，现有测试只覆盖了"查不到"和"证明未发送"两条路径；**"查到同一订单并合并"的路径无测试**。
2. **"发现仍活动的订单可消除'是否被受理'的未知状态"无正例**。
3. **"人工点击恢复不能释放预留"无测试**。`RESUME` 控制命令与预留释放的联动没有任何断言。
4. **EXE-01"先持久化再调用 Bybit"只有替身内断言**：`ControlledPaper.create_order` 里 `assert row.status == "SUBMITTING" and row.sync_state == "PENDING"` 且预留存在 —— 这确实是有效证据（它证明了调用时 DB 已有记录），但它依赖测试替身实现，不是对生产 `Executor.submit` 顺序的独立断言。

---

### AC-09 · EXE-01 / EXE-02 / LED-02 —— 部分验证

**已取证**
- 崩溃点①（持久化前）：`test_prepared_unsent_recovery_releases_cash_units` —— 没有订单记录，仅有预留，恢复时释放现金单位。
- 崩溃点②（持久化后、发送前）：`test_recovery_orphan_reservation_proves_unsent_and_cannot_revive` —— 恢复流程证明未发送、释放预留、禁止复活。
- 离线成交与水位：`test_reconciliation_retains_offline_fill_and_watermark` —— 7 天前的离线成交被补记（`repaired=True`），第二次对账水位前移且 `repaired=False`，`Execution` 只 1 条，`ReconciliationRun` 2 条。
- 跨重启不重复开仓、不漏记成交：`test_start_baseline_takeover_trade_restart` —— 重启后 `Execution` 仍为 1 条，模拟余额与检查点一致。

**剩余差距**
1. **崩溃点③④完全缺失**：AC-09 要求"在**发送后未保存响应**、**部分成交后**分别终止进程"，这两个点没有任何测试。现有 `TimeoutError` 是 `submit` 抛异常，不是进程被杀。
2. **没有真实进程级崩溃演练**。所有"崩溃"都是同进程内构造状态。需求 §7 要求"输入、操作过程、预期结果、实际结果、日志/数据库证据和软件版本"，`kill -9` 级别的演练尚不存在。
3. **"未知结果保持暂停"** 在单测中通过 `RiskPause(reason_code="UNKNOWN_ORDER")` 断言，但**跨重启后仍处于暂停**没有断言（AC-18 的锁定语义同样缺这条）。

---

### AC-10 · EXE-03 / LED-01 —— 部分验证

**已取证**
- `test_terminal_before_fill_preserves_reservation_and_no_double_count`：终态先到（`Cancelled` + `cumExecQty=0.4`）→ **预留不释放**；随后到达 0.4 的成交 → 入账且**重复应用无效**（`Execution` 只 1 条）；再用旧快照（`New`, 0 成交）回放 → **终态不被倒退**（仍 `CANCELED`，`qty_filled=0.4`，`sync_state=SYNC_CONFIRMED`）；预留 `consumed=4.01 / released=6.99`；库存 0.4。
- `test_late_fill_after_cancel_reclassifies_release_and_upgrades_full`：撤单后晚到成交 → 状态升级为 `FILLED`、`qty_filled=1`，预留 `consumed=10.1 / released=0.9`。
- `test_terminal_and_fill_monotonic_state`：状态机单调性；`EXPIRED` 在未确认时抛 `TransitionError`（对应"EXPIRED 只用于已确认的交易所失效事实"）。
- `test_private_events_dedupe_category_and_do_not_lose_other_category`：私有流同 `execId` 去重但**不丢另一品类**。
- `test_private_invalid_execution_does_not_enter_dedupe_cache`：非法成交不进去重缓存。

**剩余差距**
1. **乱序只覆盖了一半**。测试的顺序是"撤单先到 → 成交后到"；**"成交先到 → 撤单后到"**没有单独用例。
2. **"最终累计成交及预留与交易所一致"没有独立核对步骤**。Mock 既是交易所又是被测对象，缺少"以交易所 `cumExecQty` 为准逐笔核对本地预留"的断言。
3. **更正事件（`correction_of_id`）无测试**。`ledger.py` 支持追加更正事件，`db/models.py:616` 有字段，但没有任何用例写入或断言一条更正事件。
4. **去重域定义与需求不完全一致**。需求要求"以**账户/环境/产品与成交编号**构成去重域"，实现是 `order_execution_dedupe_key(exchange_exec_id)`（`ledger.py:515`），即单一全局键。这在单账户单环境首期不影响正确性，但与需求文字存在偏差，应在评审中确认是"实现超出/偏离"还是"需求表述待修订"。

---

### AC-11 · EXE-04 —— 部分验证

**已取证**
- `test_read_retries_bounded_and_limit_headers_recorded`：读请求有界重试（2 次退避后成功），并记录 `X-Bapi-Limit` / `X-Bapi-Limit-Status`。
- `test_long_server_cooldown_fails_without_retry_storm`：429 + `Retry-After: 60` → 抛 `RateLimited`，**只调用 1 次**，不形成请求风暴。
- `test_transport_read_failure_retries_only_configured_attempts`：传输层失败只重试配置次数。
- `test_read_permission_failure_is_not_retried_and_raw_message_redacted`：权限错误（`retCode=10005`）**不重试**，原始报文不外泄。
- `test_ambiguous_writes_sent_once`：写请求不盲重试。
- `test_exit_rebuild_preserves_deadline_quantity_and_floor`：撤单确认后重建为**子订单**（`parent_order_id`、`rebuild_index=1`），数量 ≤ 原意图数量，价格 ≥ 底线，`local_ttl_expires_at ≤ intent.expires_at`；重建次数用尽后不再产生第三个订单。
- `test_cancel_ack_without_terminal_preserves_held`：撤单回执未确认终态 → 预留保持占用。

**剩余差距**
1. **"子订单总量不突破意图上限"未被断言**。`ExecutionConfig.max_intent_total_spend` 是需求明确要求的"累计成交、未完成数量和最大支出不能超过原意图边界"，但测试只断言 `child.qty <= exit_intent.qty`，**没有断言累计支出 ≤ `max_intent_total_spend`**。
2. **"本地订单 TTL 到期只触发撤单"无测试**。`_cancel_expired` 的 `expired` 分支（`service.py:1585`）无断言。
3. **"停机错过 TTL"无测试**。
4. **"信号有效期只控制新提交"无测试**。
5. **"撤单查询时限"**（`cancel_query_deadline_seconds`）在测试里被设为 0（`make_executor(cancel_verify_timeout=0)`），**超时路径从未被执行过**。

---

### AC-12 · EXE-05 / RSK-03 —— 部分验证

**已取证**
- `test_lease_single_holder_and_no_auto_takeover`：第一个 `try_acquire()` → `NEEDS_CONFIRMATION`，第二个 → `HELD_BY_OTHER`；重复 `heartbeat()` 不产生第二行；`confirm_takeover(operator, evidence)` 后 → `ELIGIBLE`；释放后第二个租约仍只能拿到 `NEEDS_CONFIRMATION`（**不会自动接管**）；人为 `pg_advisory_unlock_all()` 后 `heartbeat()` → `LOST`。
- `test_remote_adapter_cannot_use_paper_label_to_bypass_lease`：标着 `environment="paper"` 的远端适配器仍被租约校验拒绝。
- `test_start_baseline_takeover_trade_restart`：重启后租约回到 `NEEDS_CONFIRMATION`，必须人工接管。

**剩余差距**
1. **"同时启动两个实例"没有真实双实例测试**。测试是在同一进程里创建两个 `ExecutorLease` 对象，**不是**两个进程/容器同时启动。NAS 上未做过双实例实测。
2. **"断开执行者数据库连接"是模拟的**。测试用 `pg_advisory_unlock_all()` 制造锁丢失，不是真实断连场景（真实断连时旧进程可能仍在运行 —— 需求强调"仅数据库锁超时不能作为旧进程已停止的证据"）。
3. **"失权时的在途请求作为待核实交易处理"无测试**。
4. **"新执行者接管前必须确认旧执行者已停止并完成对账"**：`CONFIRM_TAKEOVER` 命令要求 `evidence` 字段（`ops/control.py` + web 层校验 `test_commands_reject_invalid_scope_and_confirmation`），但**"完成对账"这个前置条件是否被强制检查没有断言**。

---

### AC-13 · OPT-01 / OPT-02 —— 部分验证

**已取证**
- `test_option_sale_requires_reduce_only_and_one_way_position`：期权卖出必须带 `reduceOnly=true` 与 `positionIdx=0`，否则 `ValueError("reduce-only")` —— 对应 OPT-01"不降级为普通卖单"。
- `test_option_close_credits_cash_and_cannot_short`：平仓回款（998.7015 USDT），持仓归零，不产生空头。
- `test_inventory_close_competition_and_strategy_ownership`：平仓授权量扣除策略归属与其他预留。
- `test_external_fill_is_recorded_but_never_owned`：外部成交入账但**不归属任何策略**，并触发 `EXTERNAL_ACCOUNT_CHANGE` 暂停。
- `test_missing_position_locks_inventory_and_keeps_cost`：持仓消失 → 库存 `trusted=False`、保留成本。

**剩余差距**
1. **"两个平仓信号竞争同一多头仓位"没有真实的平仓授权竞争测试**。`test_inventory_close_competition_and_strategy_ownership` 测的是**库存预留**竞争，不是**平仓授权**（账户多头 ∩ bot 接管 ∩ 策略归属 三层取最小）竞争。
2. **"叠加部分成交、未知卖单和撤单竞态"未组合测试**。三个因素单独有测试，组合没有。
3. **"外部改仓时停止自动卖出"链路无测试**。库存 `trusted=False` 有断言，但"因此自动卖出被拒绝"的服务层路径没有。
4. **"跨策略减仓须显式控制命令指定分摊数量"无测试**。`REDUCE` 命令只测了输入格式校验（`test_reduce_requires_positive_finite_exact_decimal_strings`）。
5. **"交易所不接受必要减仓语义时禁用该功能"** 适配器层拒绝有测试，但"环境能力不足 → 禁用该产品"（`InsufficientCapability` 传播到产品禁用）无测试。

---

### AC-14 · RSK-05 / OPT-03 —— 部分验证

**已取证**
- `test_opposite_orders_cannot_offset`：两张方向相反的候选单不能互抵，`D_min=-2 / D_max=+2`，超出 `L=1` → 拒绝。对应需求"不能依靠两张方向相反的挂单同时成交来抵消风险"。
- `test_unquantified_delta_rejected`（3 参数：None / NaN / Infinity）→ `DELTA_UNQUANTIFIED`。
- `test_risk_uses_cash_and_two_sided_per_base_delta`：按**标的**分别归一化的双边 Delta 限额；`ETH` 的 100 单位不计入 `BTC` 限额。
- `test_missing_option_greeks_never_zero_delta`：期权 Greeks 缺失 → 不按 0 处理。

**剩余差距**
1. **AC-14 点名的几个场景没有独立用例**：大负 Delta、已确认的外部库存计入账户 Delta、相反挂单只有一侧成交、现货新单改变账户 Delta。
2. **权利金预算 / 到期集中度 / 价格边界超限的拒绝路径完全无测试**（`PREMIUM_BUDGET`、`PREMIUM_CONCENTRATION_UNDERLYING`、`PREMIUM_CONCENTRATION_EXPIRY`、`PRICE_DEVIATION` 四个检查码在 `tests/` 中一次都没出现）。这正是 AC-14 后半句"权利金、到期集中度、价格边界超限同样拒绝"的要求。
3. **"期权数量、报价及 Greeks 单位映射必须用样例验证"未做**。`runtime.option_units_verified` 是唯一的开关，但没有验证样例，也没有测试证明"未验证时相关规则不执行"（代码在 `service.py:786-795` 会把估值置为不完整）。
4. **"减仓使 Delta 更偏离时展示影响、告警并暂停新增风险"无测试**。
5. **"Gamma/Vega/Theta 首期记录展示"**：字段确实记录（`market.py:311-313`、`ledger.py:473-475`、`db/models.py` 的 `Position.gamma/vega/theta`），但**没有验收断言**，也没有在管理页上验证展示。

---

### AC-15 · OPT-04 / LED-01 —— 部分验证

**已取证**
- `test_paper_settlement_checkpoint_and_ledger_replay[90-0]`（价外零支付）与 `[120-2]`（价内现金结算）：到期后 `SETTLEMENT` 事件恰好 1 条，库存清零，余额 `before + payout`，成交记录未被重复记账。
- `test_settlement_evidence_idempotency_and_cost_release[0/20]`：证据不完整（缺 `balance_reconciled`/`positions_reconciled`）→ `ValueError("完整余额")` 且**库存保持 1 不释放成本**；证据完整 → 事件写入、`released_cost` 正确；**重复调用返回 None（幂等）**；同一 `settlement_id` 但金额不同 → `ValueError("事实冲突")`。
- `test_invalid_book_and_non_touch_do_not_fill`：跳空盘口不产生成交。

**剩余差距**
1. **禁开仓窗口的四条要求全部无测试**：
   - 窗口内拒绝买入 —— `risk/engine.py:478-489` 的 `NO_OPEN_WINDOW` 检查码在 `tests/` 中一次都没出现。
   - "相关开仓单的本地 TTL 不得晚于窗口起点" —— `service.py:1391-1398` 有实现，无断言。
   - "进入窗口撤销仍活动的 bot 买单并核对撤单竞态和晚到成交" —— `_cancel_expired` 里 `order.category == "option" and window` 分支（`service.py:1593`）有实现，无断言。
   - "未能撤销/退出时告警且展示余量，不宣称已消除风险" —— `CANCEL_PENDING` fault 有实现，无测试。
2. **到期结算用的是合成 fixture**，不是真实交割结构。`evidence.source = "official_fixture"`，与需求 §7"非实盘无法产生的到期结算等场景用固定真实结构样例验证解析与记账"尚有距离 —— 样例并非真实结构。
3. **"待结算不释放预算"已被 `test_settlement_evidence_idempotency_and_cost_release` 覆盖**（证据不足 → 库存保持 1），这是本条中证据最好的子项。

---

### AC-16 · LED-02 / LED-03 / OPT-02 —— 部分验证

**已取证**
- `test_external_fill_is_recorded_but_never_owned`：外部成交 → `Inventory` 0 条 + `EXTERNAL_ACCOUNT_CHANGE` 暂停。
- `test_external_cash_change_cannot_be_clean_recon`：外部资金变化 → `allows_resume=False` + `EXTERNAL_CASH_CHANGE` 差异。
- `test_missing_position_locks_inventory_and_keeps_cost`：持仓消失 → 库存 `trusted=False`、成本保留。
- `test_reconciliation_retains_offline_fill_and_watermark`：水位与重叠窗口。

**剩余差距**
1. **"默认不接管或撤销人工订单"无断言**。`reconcile_orders` 会记录 `EXTERNAL_ORDER` 差异，但是否发出过撤销请求无测试。
2. **"人工确认后有审计地重建基线"整链无端到端测试**。`CONFIRM_TAKEOVER` / `INITIALIZE_BASELINE` 两条命令各自有入队幂等测试（`test_all_commands_only_enqueue_once`），但"外部变化 → 暂停 → 人工确认 → 重建基线 → 恢复开仓"没有串起来验证。
3. **"导入已有库存必须明确数量、成本来源、归属及审计记录"无测试**。`Inventory.cost_source` 字段存在（`test_costs_follow_fee_units_and_strategy` 用 `bot_fill`），但"外部导入"路径无用例。
4. **交易所手动下单/改仓/划转只以合成的 `ExecutionView`/`AccountSnapshot` 覆盖**，未对接真实接口。

---

### AC-17 · LED-04 / RSK-03 —— 部分验证（估值口径已补测，其余仍待补）

**已取证**
- `test_start_baseline_takeover_trade_restart`：启动后 `baseline_ok=False` → 必须显式 `INITIALIZE_BASELINE` → `baseline_ok=True`；`EquityBaseline` 恰好 1 条；重启后 `baseline_ok` 仍为真。
- `test_external_cash_change_cannot_be_clean_recon`：外部资金变化 → 不允许恢复。
- `test_loss_pause_does_not_block_trusted_reduce`：亏损触发时可信减仓仍放行。

**剩余差距（本条缺口最多）**
1. ~~**`valuation.py` 零测试**~~ **本轮已关闭**。新增 `tests/test_valuation_secrets.py` 覆盖 `value_account`（自有净现金 + 期权价值、负债即不完整、缺汇率、缺标记价、合约乘数、空头不计值、缺 `wallet_balance`）、`day_pnl`（入金/提现两种符号）、`adjusted_equity`、`drawdown_amount`（不为负）、`day_key`/`day_start` 跨时区日界线。此前无任何可执行证据表明实现符合 §4.8 口径，现在有了。
2. **"重启不重置高点"无跨重启断言**。`drawdown_peak` 在 `_baseline` 里继承 `previous.drawdown_peak`（`service.py:895`），但没有"重启后回撤高点不变"的测试。
3. **"日初基线缺失时拒绝开仓"无直接断言**。
4. **"首次接入显式初始化并标记不完整交易日"** —— `establish_baseline` 写了 `incomplete_day: True`（`service.py:956`、`web/app.py:392`），前端也区分展示；但**没有测试断言该标记被持久化并在状态页正确区分**。
5. **"失效估值不解除暂停"与实现存在语义冲突，需评审**。`VALUATION_UNAVAILABLE` 被列在**技术暂停**（`ops/pauses.py:53-61` 的技术类，不在 `LOCKED_REASONS` 内），`resolve_technical` 会自动解除它；而需求 §4.8 明确写"数据不完整时展示'估值不可用'，不能显示为零风险**或用旧净值解除熔断**"。当前 `service.py:798-801` 在估值不完整时写入 `VALUATION_UNAVAILABLE`，随后 `_clear_technical` 有可能在其余条件满足时解除它。**这一处需要按需求原文裁决实现或修订需求，并补测试固定行为。**
6. **"费用/期权价值不双算""跨币费用""标记价与买价明显不同"无测试**。
7. **"估值政策、日界线或起始基线变更仅在暂停状态有审计地执行"无测试**。
8. **"日界线切换只更新日统计，不自动解除已触发的暂停"** —— 熔断原因在 `LOCKED_REASONS` 内所以不会被自动解除，但**没有换日测试**来证明这一点。

---

### AC-18 · RSK-04 / OPS-02 / OPS-03 —— 部分验证

**已取证**
- `test_loss_pause_does_not_block_trusted_reduce`：`daily_pnl=999 / drawdown=999` 且账户暂停原因含 `DAILY_LOSS_LIMIT` 时，可信减仓仍 `approved=True`；但 `identity_trusted=False` 时连减仓也拒绝 —— 对应 RSK-04"减仓仍必须满足身份、执行权、可信持仓、数量预留、价格边界和审计条件"。
- `test_all_commands_only_enqueue_once`：7 类命令并发只入队一次，`status=PENDING`，`requested_by` 记录，**重复点击不重复生成执行意图**。
- `test_commands_reject_invalid_scope_and_confirmation`：缺确认/确认文案不对/非法 `command_id`/越权命令类型/重复 order_link_id/空列表+`isLeverage=1` 全部 422。
- `test_completed_command_preserves_actual_results`：已完成命令展示真实结果（`terminal_confirmed` 与 `pending` 分开）。
- `test_status_is_environment_scoped_and_allowlisted`：暂停原因展示且敏感诊断内容被隐藏。

**剩余差距**
1. **"触发亏损阈值后停止开仓并撤销 bot 买单"端到端无测试**。风控层有 `LOSS_CIRCUIT_BREAKER` / `DRAWDOWN_LIMIT` 检查，服务层的"暂停 → `_cancel_expired` 撤销 bot 开仓挂单"链路无断言。
2. **"数据库不可写时不发新的买卖单"无测试**。
3. **"重启/换日不解除锁定"无跨重启断言**。实现上 `RiskPause` 落库且 `resolve_technical` 只解 `locked=False` 的行，逻辑正确，但**没有测试证明重启后暂停仍在**。
4. **"人工恢复前必须对账通过"无测试**。`RESUME` 的 `resume_handler` 具体前置校验未被断言。
5. **"技术暂停须经重新同步及全部检查通过才可解除"** —— `_clear_technical` 有调用点，但**"全部检查通过"这个条件本身没有被测试**。

---

### AC-19 · OPS-01 / OPS-02 —— 部分验证

**已取证**
- `test_status_is_environment_scoped_and_allowlisted`：状态接口按环境隔离（live 环境的数据不出现）、返回账户币种与可用额、基线可用性、日损益为 `None` 时如实返回、暂停原因脱敏、对账差异明细、行情 `stale` 标记、合约 `base_coin`；并断言 `sensitive-` / `other-live-` / `db_dsn` / `credential_ref` / `password_hash` 均不出现在响应中。
- `test_csv_trace_joins_and_escapes_formulas`：CSV 追溯到 `signal_id`、`strategy_version`、`risk_config_version`、`order_link_id`、`exchange_exec_id`、`fee`、`fee_asset`；公式注入被转义（`'=HYPERLINK`）；live 环境行与 `csv-secret` 不出现；超限 `limit` 返回 422 且 `x-export-truncated` 头正确。
- `test_console_requires_session_and_serves_local_assets`：页面展示"首次风险基线""最近行情时效"；CSP `script-src 'self'`、`cache-control: no-store`、`x-frame-options: DENY`；`/openapi.json` 404。
- `test_all_commands_only_enqueue_once`：重复点击只产生一个控制命令。

**剩余差距**
1. **状态页字段虽齐全，但测试断言不完整**。状态接口实际返回 `risk_rejects`、`orders`、`executions`、`alerts`、`reconciliations[].differences`、`quotes[].stale`、`instruments`、`strategies`（含版本）等，**但这些集合除 `instruments` 与 `quotes` 外都没有内容级断言**。
2. **"所有金额带币种"没有逐项断言**。实现上 `currency` 出现在 `data["currency"]`、`inventory.currency`、`instrument.quote_coin` 等处，但订单/成交金额是否都带币种未验证。
3. **"未知订单"在状态页的呈现无断言**（`sync_state=UNCERTAIN` 的订单在 `orders` 集合里，但测试未构造该场景）。
4. **"数据年龄"**：`age_seconds` / `stale` 已实现并对 quotes 有断言，**账户快照的 `age_seconds` / `stale` 无断言**。

---

### AC-20 · OPS-04 / OPS-05 / NFR-03 —— 部分验证

**已取证**
- `test_delivery_retry_dedupe_and_resolution`：投递失败落库（`delivery_attempts=1`）且**错误信息不含通知 token**；恢复后可投递；重复 `raise_alert` **保留首次 `first_seen_at`** 且状态回到 `DELIVERED`；`resolve` 后重新出现会开新 episode（`history` 长度 2、`delivered_channels` 清空）—— 对应"告警去重、保留首发与恢复事件"。
- `test_independent_monitor_three_misses_one_alert_and_recovery`：连续 3 次、间隔 30 秒的心跳缺失 → **只发 1 条告警**；恢复后再发 1 条恢复事件，且 `consecutive_failures` 归零。
- `test_offsite_backup_staleness_is_independent_of_health`：备份过期与健康状态**互相独立**判定。
- `test_backup_age_uses_dump_time_instead_of_copy_time`：备份年龄用**dump 时间**而非复制时间。

**剩余差距**
1. **真实通知渠道未配置**（`ops.alert_channels: []`）。投递逻辑只在 `httpx.MockTransport` 下测过，**没有任何一次真实投递**；`deploy/` 下也没有企业微信/Telegram/邮件的真实通道配置。
2. **"关键告警超时未送达时暂停新增风险"无测试**。`service.py:1913-1919` 有实现（`NOTIFICATION_FAILURE` 暂停），无断言。
3. **"告警含原因和剩余仓位"无断言**。`AlertManager.raise_alert` 支持 `context`，但"剩余仓位"是否进上下文没有测试。
4. **8 类告警触发源未逐项测试**：账户异常、未知订单、对账差异、断流、API 限流/权限失败、退出未完成、到期未结算、磁盘/数据库故障。目前只有 `UNKNOWN_ORDER`、`EXTERNAL_ACCOUNT_CHANGE`、`DISK_WARNING`、`DISK_PRESSURE`、`CLOCK_*` 在代码里定义，测试只覆盖了前两个。
5. **外部监控未实际运行**（Mac 后台监控仍在修复），见 §4。

---

### AC-21 · SEC-01 / SEC-02 / SEC-03 —— 部分验证

**已取证**
- 认证与会话：`test_console_requires_session_and_serves_local_assets`（未登录 401/303、CSP、`no-store`、`DENY`、openapi 404）、`test_login_cookie_and_error_responses_hide_password`（`Secure` / `HttpOnly` / `SameSite=strict` / `Max-Age=1800`，错误响应不回显密码与 hash）、`test_sessions_expire_absolutely_and_logout_revokes`（绝对过期、登出吊销）、`test_web_restart_invalidates_old_session`（重启使旧会话失效）、`test_login_requires_exact_origin`（4 参数）、`test_login_rate_limit_is_bounded_and_recovers`（限流 429 + `retry-after=300`，窗口过后恢复）。
- 写操作保护：`test_control_requires_origin_and_csrf`（4 参数：正确 Origin 缺 CSRF → 403；错 CSRF → 403；错 Origin → 403；`Sec-Fetch-Site: cross-site` → 403；越权 `/api/logout` → 403；**失败时不产生任何 `ControlCommand`**）。
- 认证文件：`test_admin_file_rejects_broad_or_wrong_mode`（3 个权限位）、`test_admin_file_rejects_symlink_and_malformed_hash`（拒绝软链接、拒绝畸形 hash）、`test_admin_generation_permissions_no_overwrite`（0600、不打印密码、拒绝覆盖）。
- 脱敏：`test_read_permission_failure_is_not_retried_and_raw_message_redacted`、`test_status_is_environment_scoped_and_allowlisted`、`test_csv_trace_joins_and_escapes_formulas`、`test_private_auth_requires_positive_matching_ack_and_redacts_response`。
- 凭据不装载：`test_public_reads_have_no_auth_and_paper_discards_credentials`；`settings/secrets.py` 实现 0600 + 属主校验 + `ApiCredentials.__repr__` 只输出前 6 位 —— **本轮已补测**：`test_credentials_reject_broad_permissions`、`test_credentials_reject_group_or_world_readable_file`、`test_credentials_reject_missing_file`、`test_credentials_reject_missing_fields`、`test_credentials_reject_invalid_json`、`test_credentials_repr_and_summary_never_show_secret`、`test_redact_removes_known_secrets_but_keeps_short_ones`。
- 测试隔离：`tests/conftest.py::block_exchange_network` 阻断一切外部 HTTP；`BYBITBOT_TESTING=1` 下实盘写被拒（`test_live_test_guard_blocks_even_explicit_enabled_mock_write`）。
- 依赖固定：`uv.lock` 存在，CI 用 `uv sync --frozen`。

**剩余差距**
1. **没有自动化密钥泄漏扫描**。AC-21 要求"扫描代码/日志/镜像/导出无明文密钥"。当前只有 `.gitignore` 规则和分散的脱敏断言，**没有扫描器**（代码、日志、镜像层、导出文件都没有全量扫描）。
2. **"数据库不向公网开放"无检查**。`deploy/quadlet/bybitbot-postgres.container` 有配置，但没有自动化断言或运行时校验。
3. **"绑定固定出口 IP"只测了解析**：确认了 `*` 通配不算绑定、CIDR 不算绑定，但**没有验证部署出口 IP 是否与 Key 绑定的 IP 一致**（NAS 的真实出口 IP 未知）。
4. **凭据轮换流程无测试**。需求要求"轮换密钥时先暂停开仓并核对在途订单，验证新凭据后再恢复；失效或泄露凭据对应的操作流程必须可执行"。`CONFIRM_TAKEOVER` / `PAUSE_NEW_RISK` 可作为流程组件，但**"轮换"这一动作本身没有流程实现或测试**。
5. **"关闭提现及不需要的划转"只被解析未被强制**：`test_wildcard_does_not_become_bound_because_real_ip_also_present` 断言 `info.withdraw_enabled` 为真值，**但没有"提现权限被打开 → 拒绝启动/告警"的测试**。这是 SEC-01 的明确要求，目前只有信息展示。
6. ~~**`settings/secrets.py` 完全没有测试**~~ **本轮已关闭**。权限过宽拒绝、属主校验、缺字段拒绝、非法 JSON、`__repr__`/`masked_summary` 不泄露明文、`redact()` 兜底脱敏均已有断言（见上方"凭据不装载"一行）。剩余未覆盖的是"凭据文件被读取后是否有任何路径把它写进日志/URL/表"——那属于缺口 1 的全量扫描范畴。
7. **"部署产物带版本号"**：CI 未做镜像构建/版本标记验证。

---

### AC-22 · NFR-01 / NFR-02 / NFR-05 —— **待验证**

**现状**：本条**没有对应的自动化测试**。191 项测试中没有任何一项测量延迟分布、CPU/内存、订阅量或磁盘增长，也没有任何一项断言容量上限。

**逐项差距**
1. **容量基线（NFR-01）**：`1 账户 / ≤5 策略 / 10 现货对 / 200 期权合约 / ≤1 意图每秒` —— 无任何测试。`settings/loader.py:212` 只对"策略 ≤ 5"做了配置校验，且该校验**本身也无测试**。
2. **性能基线（NFR-02）**：`P95 ≤ 500ms` —— 无测量脚本、无基准测试、无历史结果。
3. **保留期（NFR-05）**：配置项 `ops.retention_days=365` / `ops.raw_market_data_retention_days=7` 存在（`settings/models.py:535-536`），但**全仓检索无任何 purge / prune / vacuum / cleanup 实现**。即"交易、配置、风控与审计记录保留至少 1 年；原始行情短期保留 7 天"目前**未实现**，账务表会无界增长。
4. **磁盘阈值**：`service._clock_and_disk` 实现了两级阈值（`disk_pause_threshold_gb` → 暂停、`disk_free_threshold_gb` → 告警），**但没有任何测试触发磁盘不足**（AC-22 明确要求"模拟磁盘不足，触发暂停且账务记录无静默丢失"）。
5. **"达到容量边界时停止新增风险，不静默丢弃账务记录"无测试**。

---

### AC-23 · OPS-05 / NFR-04 / LED-02 —— 部分验证

**已取证**
- `deploy/backup.sh`：`pg_dump -Fc` + `gpg --encrypt --recipient-file` 加密 + `umask 077` + `.partial` 原子改名。
- `test_backup_age_uses_dump_time_instead_of_copy_time`：备份年龄用 dump 时间戳。
- `test_offsite_backup_staleness_is_independent_of_health`：备份过期独立判定。
- `deploy/io.bybitbot.offsite-backup.plist`：每 30 分钟 rsync 拉取加密备份（`--chmod=F600,D700`）。
- 主会话已人工完成"加密异机备份恢复至独立本地库"（见 §4）。

**剩余差距**
1. **没有自动化恢复演练**。AC-23 要求"从备份恢复，回填备份后交易并核对余额/持仓/预留"。目前是人工一次性验证，**没有可重复执行的脚本或测试**。
2. **RTO ≤ 2 小时 / RPO ≤ 24 小时没有测量记录**。需求 §6 明确说这些是"拟定工程验收基线，不是当前实测结果"，本阶段仍无实测。
3. **"未能确认历史完整性时不恢复交易资格"无测试**。
4. **"恢复交易资格另行验证"未验证**。
5. **`deploy/backup.sh` 只备份 `bybitbot_paper` 库**（`pg_dump -d bybitbot_paper`）。若将来启用 testnet/live，需要按环境拆分，脚本目前是单环境硬编码。

---

### AC-24 · ENV-02 / OPT-01 / OPT-03 / OPT-04 —— **待验证**

**现状**：只有**接口契约级**证据（MockTransport），**没有任何真实环境证据**。

**已取证的契约（仅证明"我们发出的请求长这样"）**
- `test_option_sale_requires_reduce_only_and_one_way_position`：`reduceOnly=true` + `positionIdx=0`。
- `test_option_specs_all_bases_paginated_and_strike_from_symbol`：`baseCoin=All` + 游标分页 + 行权价/类型/结算币从响应解析。
- `test_option_bulk_ticker_mark_iv_and_depth_cap`：`markIv` / `delta` / `bid1Price` / `ask1Price` / 深度 `limit=25`。
- `test_option_empty_registry_is_not_complete_history`：期权基础币目录为空 → 抛 `InsufficientCapability("coverage")`，**不把空目录当完整历史**。
- `test_wallet_nested_net_cash_and_uid`：钱包嵌套结构、`accountType=UNIFIED`、`totalEquity`、`marginMode`、`unifiedMarginStatus` 映射。

**剩余差距（全部需要真实凭据 + 真实环境）**
1. **未提供任何真实 Bybit Key**，因此下列全部未实测：期权交易权限、只读/交易权限组合、`reduceOnly` 在真实环境的接受情况、持仓模式（逐仓/组合）、期权到期交割的真实结构与字段、结算币种、Greeks 单位与符号、真实费用率。
2. **"不支持项记录为未通过，不能以现货成功替代期权验收"** —— 符合现状（未做即未通过），但也意味着 M3 未达成。
3. **Demo 的能力边界未核实**。需求 §4.1 明确提醒"Bybit 的 Demo 使用独立账户和密钥，且并非支持全部接口"。代码已按"demo 公共读走 `api.bybit.com`、私有读走 `api-demo.bybit.com`"分流（`test_demo_routes_public_and_private_to_separate_hosts`），但**该分流是否与官方当前文档一致、demo 是否真的不支持期权减仓/结算，均未核对**。
4. **"部署必须按所选环境的官方说明验证，而不能只切换一个 `testnet` 布尔值"** —— 代码层已做到端点/凭据/数据库/日志标识四分离（`EnvironmentProfile`），但**没有一份按官方说明逐条核对的验证记录**。

---

### AC-25 · EXE-03 / SPT-02 / OPT-02 / LED-04 —— 部分验证

**已取证**
- `test_costs_follow_fee_units_and_strategy`：移动加权成本（买 2@10 手续费 0.1 BTC → 均价仍 10、数量 1.9）；卖出 0.9@20 → 已实现盈亏 `18 − 20/1.9×0.9 − 0.2`；剩余数量 1。**手续费币种从 BTC 换到 USDT 时公式正确**。
- `test_inventory_close_competition_and_strategy_ownership`：策略归属库存与可平量（扣除未完成预留后 0.3）。
- `test_settlement_evidence_idempotency_and_cost_release`：期权成本在结算证据完整后一次性释放 `10.000000000000000000`。
- `test_paper_settlement_checkpoint_and_ledger_replay`：权利金支付 → 到期现金结算的合成链路。

**剩余差距**
1. **"同合约两策略分批买入再部分平仓"没有测试**。所有成本测试都是**单策略**。需求明确要求"不同策略成本不混合"，但从未在双策略场景下验证过。
2. **"费用币种不同"** 只在单笔里换了一次手续费币种，没有跨币种组合（如权利金用 USDT、结算用 USDC）场景。
3. **"权利金支付 → 持仓估值 → 平仓/到期现金"全过程没有端到端断言**，且依赖未验证的 `valuation.py`（见 AC-17 缺口 1）。
4. **"无重复资产估值"无测试**。

---

### AC-26 · NFR-06 / STR-02 —— **待验证**

**现状**：**`src/bybitbot/clock.py` 的关键符号零测试覆盖**（测试只 `from bybitbot.clock import now_utc`；`monotonic` / `Deadline` / `backoff_delay` / `ClockSkewMonitor` / `local_day_start_utc` 全部无断言）。

**已存在的实现（无任何断言）**
- `clock.monotonic()` / `Deadline`：超时与退避基于单调时钟。
- `clock.backoff_delay()`：指数退避 + 抖动，且**用稳定哈希而非内置 `hash()`**（注释说明是为了重启后序列可复现）—— 这个设计决策本身没有测试保护。
- `clock.ClockSkewMonitor`：与交易所时间偏差检测，超过 `max_skew_seconds` 返回不可用。
- `clock.local_day_start_utc` / `local_day`：日界线。
- `service._clock_and_disk`（`service.py:492-507`）：墙钟跳变检测（`jump > max_clock_skew_seconds` → `CLOCK_JUMP` 锁定暂停）+ 每 60 秒与交易所对时（超限 → `CLOCK_SKEW` 暂停）。

**剩余差距**
1. **AC-26 的四项场景全部无测试**：时钟前跳、时钟回拨、超出允许偏差、恢复后仍需满足对账与暂停解除条件。
2. **"暂停新开仓"是端到端行为，无断言**。`CLOCK_JUMP` / `CLOCK_SKEW` 落在 `LOCKED_REASONS` 里所以不会被自动解，但没有测试证明"跳变 → 暂停 → 不再开仓 → 重启仍在暂停"。
3. **"调度不重复补做"覆盖不全**。`test_schedule_100_duplicates_claim_once_and_skip_missed` 证明了错过时刻被 `SKIPPED` 且不被领取，但**没有模拟时钟回拨后同一计划时刻被再次触发的场景**（这正是需求担心的"时钟回拨重复执行"）。
4. **"超时和退避时长不被墙上时钟跳变破坏"无测试** —— `Deadline` / `backoff_delay` 是这条需求的唯一实现，却零覆盖。

---

### AC-27 · RSK-02 / OPT-03 / ENV-02 —— 部分验证

**已取证**
- `test_wallet_debt_not_double_counted_or_spendable`（2 参数）：`spotBorrow=100, borrowAmount=140` → 负债取 140；`borrowAmount=0` → 负债取 100（语义不双算）；两种情况下 `available == 0`（负债不吃自有现金）。
- `test_portfolio_unknown_holds_have_zero_spendable_cash`：组合保证金下 `totalOrderIM` 缺失 → 可用现金 0（保守）。
- `test_cash_freeze_overlap_requires_order_evidence`：无法证明自有现金 → `ReservationError("缺少自有现金")`。
- `test_cash_reservation_competition`：预留竞争不会双花。

**剩余差距**
1. **"账户抵押品充足但结算币现金不足，期权买单被拒绝"没有专项测试**。现有测试都走现货现金路径；期权权利金走**结算币现金**的路径（`OPT-03`：`max_settle_coin_cash_ratio`）没有专门用例。
2. **"运行中注入新增负债，暂停并告警，不自动借贷或划转解决"端到端无测试**。`ACCOUNT_DEBT` 检查码存在，但 `tests/` 中一次都没出现。
3. **"不自动借贷或划转解决"无独立断言**。`test_spot_positions_do_not_call_unsupported_api` 断言的是"现货持仓不调用持仓接口"，**不是**"从不调用借贷/划转接口"。
4. **"已有预留与费用余量正确扣算"** —— `FEE_BUFFER` 检查码在 `tests/` 中无断言。

---

### AC-28 · STR-05 —— 部分验证

**已取证**
- `test_next_book_depth_shared_cancel_and_checkpoint`：下单后**当前盘口不成交**，下一份盘口（1 秒后）才成交；**深度 0.4 被两个订单共享扣减**（只有 `a` 成交 0.4）；手续费 `0.0004 BTC`（= 0.4 × 0.001）；余额扣减正确；未成交部分保留；`cancel_order` 后 `b` 才能成交；**状态可 JSON 导出/恢复且完全一致**；恢复后不重复成交（`exec_id` 唯一、累计数量正确）。
- `test_invalid_book_and_non_touch_do_not_fill`：买限价 100 未触及卖盘 101 → 不成交；`gap_detected` 盘口 → 不成交。
- `test_unknown_registered_and_survives_json_restore`：`inject_fault("unknown_result")` → `OrderUncertain`；订单仍登记在册；JSON 恢复后挂单与成交仍在。
- `test_paper_guard_blocks_remote_writes`：paper 不发远程写请求。

**剩余差距**
1. **"固定盘口、费用和事件顺序下模拟结果可重复"没有重放断言**。有 checkpoint/restore 一致性断言，但没有"同一事件序列跑两次 → 结果完全相同"的测试。
2. **"报告标记模拟及模型限制"无断言**。状态接口里有 `source: "paper 模拟"`（`test_status_is_environment_scoped_and_allowlisted` 构造了该字段但**没有断言它出现在响应里**），报告层面的模拟标记与模型限制说明无测试。
3. **"故障实验报告与策略表现报告分别标识"未实现** —— 检索全仓没有区分这两类报告的机制。
4. **故障注入只用了 `unknown_result` 一种**。需求要求"支持部分成交、取消及结果未知等故障注入"，`inject_fault` 具备该能力但没有覆盖另外两种的测试。
5. **"手续费使用明确版本的保守费率"** —— `FeeSchedule(version=...)` 存在且被 `test_next_book_depth_shared_cancel_and_checkpoint` 间接验证（0.001 费率），但**费率版本号本身未被断言**。

---

## 4. 已在主会话核验的运行事实（与测试证据分列）

以下事实由主会话在真实运行环境中核验，**不属于自动化测试证据**，单独列出以免与 §2 的测试矩阵混淆：

| 项 | 核验结果 | 对验收的意义 |
|---|---|---|
| 本地隔离测试 | **191 项全部通过**（与本文 §0 的 `pytest --collect-only` 计数一致） | 证明当前代码在合成环境下自洽；**不证明**任何真实交易所行为 |
| 静态检查 | `ruff check src tests deploy` 通过 | 无 lint 违规 |
| 数据库迁移 | Alembic head = `786d42c1b808`；`alembic check` 无差异 | schema 与模型一致，无待生成迁移 |
| NAS 镜像 | `910874d2cf3b853ad9758f49b95a812eb2561a141aa5969f0eb79202f8c787c1` | 产物可追溯到唯一镜像 |
| NAS 运行态 | paper 模式；`PAUSED_OPEN` / `NEEDS_CONFIRMATION`；USDT 1000；`baseline=false`；两策略 disabled；`executions=0` | **正确体现"未启用策略、未建立基线、不发单"的安全默认态**；同时说明 NAS 上**至今没有产生任何一笔成交**，因此 §2 中所有执行链路结论均来自 paper 模拟 |
| 管理页面 | HTTPS 认证登录 / 401 未授权 / `Secure` `HttpOnly` `SameSite=Strict` / CSRF / 退出已核验 | 与 AC-21 的测试证据方向一致（真实 HTTPS 入口已验，**但** AC-21 的自动化扫描项仍未覆盖） |
| 备份 | 加密异机备份已恢复至**独立本地库**并验证 | 支持 AC-23 的"备份可用性"，但**恢复演练仍不可重复执行**，RTO/RPO 仍无实测 |
| Mac 后台监控 | **已投运**：launchd 任务 `io.bybitbot.nas-heartbeat`（30 秒）与 `io.bybitbot.offsite-backup`（30 分钟）均 `last exit code = 0`，心跳状态文件显示 `healthy: true`，加密备份已实际复制到 Mac | 外部监控的**进程与产物**已核实；**告警送达**仍未验证（连续 3 次失败的通知路径未做真实触发演练），且监控依赖这台 Mac 在线 |
| 真实 Bybit 凭据 | **未提供** | AC-24 与所有"真实环境能力"全部未验证 |
| Demo/Testnet 能力 | **未验证** | M3 未达成 |
| 实盘授权 | **未授权**（`live_gates` 三项皆空） | AC-01 的 ENV-03 部分、M4a/M4b 全部未开始 |
| 真实远端告警 | **未配置**（`alert_channels: []`） | AC-20 的真实投递未验证 |
| GitHub | **本地已提交**（首个提交 `5a09d39`，分支 `feature/m0-m3-paper-deployment`）；**远端未创建、未推送**（`gh` 仍未完成登录） | `.github/workflows/test.yml` 的 CI **从未在远端执行过**，其结论不能当作已验证证据 |

---

## 5. 建议的补验顺序

### 第一批：补零覆盖的高价值模块（成本低、收益高）

| 序 | 目标 | 新增/修改 | 关闭的缺口 |
|---|---|---|---|
| 1 | ~~`valuation.py` 单元测试~~ **已完成**（`tests/test_valuation_secrets.py`） | 已新增 27 项断言 | G1 已关闭 |
| 2 | `clock.py` 单元测试 | 新增 `tests/test_clock.py`：前跳、回拨、超偏差、`Deadline` 不受墙钟影响、`backoff_delay` 稳定性与抖动边界 | G2 / AC-26 |
| 3 | 配置校验测试 | 新增 `tests/test_settings.py`：`ConfigError` 断言（独立 DSN、live 缺 UID、币种不一致、策略 > 5、禁开仓 TTL ≥ 订单 TTL、`LiveGateConfig` 三件套、`EnvironmentProfile` paper 禁凭据） | AC-01 / AC-06 |
| 4 | 风控拒绝分支 | 在 `tests/test_core.py` 补参数化用例，逐个检查码断言拒绝与 `reason_code` | G3 / AC-06 / AC-14 / AC-15 / AC-27 |
| 4b | 凭据文件安全 | 新增 `tests/test_secrets.py`：权限过宽（0644/0666/0604）拒绝、属主不符拒绝、缺 `api_key`/`api_secret` 拒绝、`redact()` 兜底脱敏、`ApiCredentials.__repr__` 不泄露 | G9 / AC-21 |

### 第二批：补服务层闭环（端到端）

| 序 | 目标 | 关闭的缺口 |
|---|---|---|
| 5 | 策略配置版本链路：变更→新版本、旧订单保留原版本、停用策略撤单、收紧限额重评挂单 | AC-06（G6） |
| 6 | 暂停链路：亏损熔断→停止开仓→撤销 bot 买单→可信减仓继续；重启/换日不解除锁定；RESUME 前置对账 | AC-18 / AC-16 |
| 7 | 禁开仓窗口全链路：拒绝买入、TTL 不晚于窗口起点、进入窗口撤单、撤单未成功告警展示余量 | AC-15 |
| 8 | 崩溃恢复演练（真实子进程 `kill -9`）：4 个崩溃点 | AC-09（G5） |
| 9 | 双实例租约实测（两个进程同时抢）+ 真实断连 | AC-12 |
| 10 | 行情失效→暂停→重建恢复 的服务层闭环 | AC-03 |

### 第三批：能力/环境类（需要外部条件）

| 序 | 目标 | 关闭的缺口 |
|---|---|---|
| 11 | 提供真实 Testnet/Demo 凭据，执行 AC-24 的期权权限/减仓/成交/撤单/结算验证，产出只读能力报告 | G7 / AC-24 / M3 |
| 12 | 性能与容量基准（P95、CPU/内存、订阅量、磁盘增长）脚本化 | G4 / AC-22 |
| 13 | 实现并测试数据保留清理（365 天账务 / 7 天原始行情）+ 磁盘不足暂停测试 | G4 / AC-22 |
| 14 | 配置真实告警渠道并做送达/失败/暂停演练；补做 Mac 端监控的告警送达演练 | G8 / AC-20 |
| 15 | 密钥泄漏自动化扫描（代码/日志/镜像/导出）+ 提现权限开启即拒绝启动 + 凭据轮换流程 | AC-21 |
| 16 | 可重复执行的备份恢复演练脚本 + RTO/RPO 实测 | AC-23 |
| 17 | 裁决"失效估值不解除暂停"的语义冲突（实现 vs 需求），并补测试固定行为 | AC-17 缺口 5 |

---

## 6. 附录

### 附录 A · 测试文件分布（191 项）

| 文件 | 项数 | 主要覆盖 |
|---|---|---|
| `tests/test_adapters.py` | 71 | Bybit V5 契约（MockTransport）、签名、端点、分页、限流/退避、WS 快照/增量/私有流 |
| `tests/test_web.py` | 42 | 管理页面认证、会话、CSRF、命令幂等、状态脱敏、CSV 审计导出、认证文件权限 |
| `tests/test_execution.py` | 19 | UNKNOWN 语义、预留生命周期、状态机单调性、租约、对账修复、结算幂等 |
| `tests/test_core.py` | 15 | Delta 区间、现金/库存预留竞争、成本与手续费、调度去重、orderLinkId |
| `tests/test_service.py` | 7 | 端到端（paper）：基线/接管/重启、定时任务只成交一次、退出重建、结算重放 |
| `tests/test_paper.py` | 4 | 模拟撮合模型：深度共享、未触价、故障注入、期权平仓 |
| `tests/test_deploy_ops.py` | 4 | 外部心跳监控（3 次丢失告警）、备份过期独立判定、备份年龄口径、监控安装位置 |
| `tests/test_valuation_secrets.py` | 27 | 估值口径（权益、日损益、回撤、日界线）与凭据装载权限/脱敏 |
| `tests/test_ops.py` | 1 | 告警投递重试/去重/恢复 |
| `tests/test_cli.py` | 1 | 管理员凭据生成（0600、不打印、拒绝覆盖） |
| **合计** | **191** | |

### 附录 B · **完全没有直接测试**的模块（本轮已补测 valuation.py 与 settings/secrets.py）

按"`tests/` 中是否 `import bybitbot.<模块>`"统计。原为 7 个模块，`valuation.py` 与 `settings/secrets.py` 已于本轮补测，现剩 5 个：

| 模块 | 行数 | 风险 | 说明 |
|---|---|---|---|
| `src/bybitbot/money.py` | 160 | 中 | 精度与舍入方向（"精度向不扩大支出的方向处理"）无直接断言，仅间接经由服务层结果观察 |
| `src/bybitbot/market.py` | 383 | 中 | 规格更新 / 退市处理 / 快照持久化无直接断言 |
| `src/bybitbot/ops/state.py` | 124 | 中 | 运行状态机（`STARTING/RECONCILING/RUNNING/PAUSED_OPEN/EXITING/STOPPED`）的转换规则无直接断言 |
| `src/bybitbot/ops/pauses.py` | 281 | 中 | 暂停的加锁/解锁、重复暂停合并、审计化人工解除均通过服务层间接使用，无直接断言 |
| `src/bybitbot/strategy/templates.py` | 254 | 中 | 候选筛选、排序决胜、规则参数化仅通过服务层间接使用，无直接断言 |

另有 1 个模块**被导入但关键符号未测**：

| 模块 | 行数 | 风险 | 说明 |
|---|---|---|---|
| `src/bybitbot/clock.py` | 161 | **高** | 测试只 `from bybitbot.clock import now_utc`；`monotonic` / `Deadline` / `backoff_delay` / `ClockSkewMonitor` / `local_day_start_utc` 全部无断言（AC-26） |

> 说明：上表不排除这些模块被其他模块间接调用过（例如 `service.py` 会调用 `value_account`、`PauseManager.pause`），但"被调用"不等于"其语义被断言"。

### 附录 C · 需求编号 → 验收用例 → 状态速查

| 需求 | 用例 | 状态 | 主要缺口 |
|---|---|---|---|
| ENV-01 | AC-01 | 部分验证 | 配置层校验无测试 |
| ENV-02 | AC-01 / AC-24 / AC-27 | 部分验证 / 待验证 | 真实 UID/权限/模式未比对 |
| ENV-03 | AC-01 | 部分验证 | LiveGate 三件套与实盘启动门槛无测试 |
| MKT-01 | AC-02 | 部分验证 | 规格变更后拒单无测试 |
| MKT-02 | AC-03 | 部分验证 | 服务层闭环无测试 |
| MKT-03 | AC-03 | 部分验证 | 决策快照引用未落表 |
| STR-01 | AC-04 | 部分验证 | 意图字段完整性无断言 |
| STR-02 | AC-04 / AC-26 | 部分验证 / 待验证 | 时区跨日、时钟回拨无测试 |
| STR-03 | AC-05 | 部分验证 | 退出规则仅测一种 |
| STR-04 | AC-06 | 部分验证 | 配置版本链路无测试 |
| STR-05 | AC-28 | 部分验证 | 重放确定性、模拟标记无断言 |
| RSK-01 | AC-06 | 部分验证 | 20+ 检查码仅 6 个有覆盖 |
| RSK-02 | AC-07 / AC-27 | 部分验证 | 负债新增端到端无测试 |
| RSK-03 | AC-03 / AC-12 / AC-17 | 部分验证 | 行情/账户暂停闭环无测试 |
| RSK-04 | AC-18 | 部分验证 | 端到端撤单与 DB 不可写无测试 |
| RSK-05 | AC-14 | 部分验证 | 权利金/集中度/边界拒绝无测试 |
| EXE-01 | AC-08 / AC-09 | 部分验证 | 顺序断言依赖测试替身 |
| EXE-02 | AC-08 / AC-09 | 部分验证 | `resolve_unknown` 成功路径无正例 |
| EXE-03 | AC-10 / AC-25 | 部分验证 | 乱序另一半未覆盖；双策略成本无测试 |
| EXE-04 | AC-11 | 部分验证 | TTL 过期、撤单查询时限、累计支出上限无测试 |
| EXE-05 | AC-12 | 部分验证 | 无真实双实例/断连演练 |
| SPT-01 | AC-02 | 部分验证 | 借贷接口禁用在途未证明 |
| SPT-02 | AC-07 / AC-25 | 部分验证 | 零碎余额未实现 |
| OPT-01 | AC-13 / AC-24 | 部分验证 / 待验证 | 环境能力未实测 |
| OPT-02 | AC-13 / AC-16 | 部分验证 | 平仓授权竞争、跨策略分摊无测试 |
| OPT-03 | AC-05 / AC-14 / AC-24 | 部分验证 / 待验证 | 价格边界/权利金拒绝无测试 |
| OPT-04 | AC-15 / AC-24 | 部分验证 / 待验证 | 禁开仓窗口全链路无测试 |
| LED-01 | AC-10 / AC-15 | 部分验证 | 更正事件无测试 |
| LED-02 | AC-09 / AC-16 / AC-23 | 部分验证 | 崩溃演练、恢复演练缺口 |
| LED-03 | AC-16 | 部分验证 | 不自动撤人工单无断言 |
| LED-04 | AC-17 / AC-25 | 部分验证 | 估值公式已补测；跨重启高点、跨币费用未测 |
| OPS-01 | AC-19 | 部分验证 | 状态页字段断言不完整 |
| OPS-02 | AC-19 / AC-18 | 部分验证 | 范围校验充分，联动端到端缺 |
| OPS-03 | AC-18 | 部分验证 | 重启/换日不解除锁定无断言 |
| OPS-04 | AC-20 | 部分验证 | 真实渠道未配置 |
| OPS-05 | AC-20 / AC-23 | 部分验证 | 监控已投运；告警送达未实测 |
| SEC-01 | AC-21 | 部分验证 | 凭据文件权限/脱敏已补测；无泄漏扫描；提现权限未强制 |
| SEC-02 | AC-21 | 部分验证 | DB 公网暴露无检查 |
| SEC-03 | AC-21 | 部分验证 | 轮换流程未实现 |
| NFR-01 | AC-22 | 待验证 | 无容量测试 |
| NFR-02 | AC-22 | 待验证 | 无性能测量 |
| NFR-03 | AC-20 | 部分验证 | 无端到端延迟测量 |
| NFR-04 | AC-23 | 部分验证 | 无 RTO/RPO 实测 |
| NFR-05 | AC-22 | 待验证 | **保留清理未实现** |
| NFR-06 | AC-26 | 待验证 | **`clock.py` 零测试** |

---

**文档结束。** 本矩阵的结论仅基于 `/Users/nicholaswang/Desktop/code/bybit` 当前工作区内容与主会话记录的运行事实；任何"实盘可用"结论都必须另行取得 §5 第三批的证据。
