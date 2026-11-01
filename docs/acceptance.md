# 当前验收矩阵（AC-01～AC-28）

核验日期：2026-11-01。依据：[需求文档](requirements.md) v0.1 的 §4、§6、§7、§8；操作步骤见 [NAS 运维手册](runbook-nas.md)。

当前交付是 M0～M3 范围内的 **paper 模拟实现和 NAS 部署**。M3 所需的真实 Demo/Testnet 联调尚未完成；本文不将模拟证据折算为真实账户能力，也不宣称全部 P0 验收通过。

## 1. 证据口径

| 状态 | 含义 |
|---|---|
| 已验证 | 对该 AC 的全部适用子场景已有可重复、通过的证据 |
| 部分验证 | 有实现及部分通过的自动化或运行证据，仍有明确未覆盖子场景 |
| 待验证 | 核心验收场景尚无有效执行证据，或依赖未具备的外部条件 |

当前统计：**已验证 0，部分验证 25，待验证 3**（AC-22、AC-24、AC-26）。每个 AC 都还有未覆盖的必要子场景，具体见矩阵；这不意味着每条 AC 都必须依赖真实交易才能测试。

本地完整回归结果为 **335 passed in 25.70s**，其中交割接口契约 116 项、交割账本对账 9 项、费用 13 项、服务集成 12 项；页面命令提示另有 Node 测试 **4 项通过**。`ruff check src tests deploy alembic` 与 `alembic check` 通过。测试数据均为合成数据，无真实交易凭据；整改及实际页面操作证据见 [2026-11-01 整改记录](remediation-2026-11-01.md)。

`tests/conftest.py` 设置 `BYBITBOT_TESTING=1` 并阻断 httpx 默认同步/异步 HTTP transport；交易所 HTTP 响应使用 `MockTransport`，模拟交易使用 `PaperAdapter`。这是应用测试层保护，不是操作系统级网络隔离。数据库用例使用独立 PostgreSQL schema。自动化测试不能据此证明真实 UID、账户模式、期权权限、报价单位、成交、撤单或到期交割能力。

需求文档末尾“当前仅交付文档”的句子属于需求撰写时点；当前已部署事实以本文第 4 节为准。

## 2. 里程碑

| 里程碑 | 当前状态 | 说明 |
|---|---|---|
| M0 开发基线 | 已建立 | 配置模式、数据模型、迁移、固定依赖、测试与部署文档已交付；GitHub Actions 已触发 |
| M1 只读接入 | 模拟与接口契约有证据，真实账户未验 | 合约、钱包、权限与身份解析已测；没有真实 Key 核对记录 |
| M2 模拟闭环 | 已实现主要链路，验收未全通过 | 持久化、预留、执行、重启、退出和 paper 结算已测；仍缺部分风控、配置变更、时钟和故障组合证据 |
| M3 非实盘联调 | 未完成 | NAS 部署、认证管理页、备份恢复已有证据；Demo/Testnet、真实告警和全部适用 P0 尚未通过 |
| M4a / M4b | 未开始 | 没有实盘凭据、启用记录或适用阶段验收证据 |

## 3. AC 矩阵

下表测试名均指当前 `tests/` 中的可执行用例。参数化用例按 pytest 收集项计数；实现存在但未断言的分支不算通过证据。

| AC / 需求 | 状态 | 已有证据 | 剩余必要子场景 |
|---|---|---|---|
| AC-01 · ENV-01/02/03 | 部分验证 | `test_endpoint_mismatch_rejected`、`test_demo_routes_public_and_private_to_separate_hosts`、`test_key_identity_fields_fail_closed`、`test_public_reads_have_no_auth_and_paper_discards_credentials`、`test_live_test_guard_blocks_even_explicit_enabled_mock_write`、`test_paper_guard_blocks_remote_writes` | 独立 DSN、live UID 与门槛的非法配置拒绝缺专项测试；实际 UID、账户模式、权限与固定出口未核对 |
| AC-02 · MKT-01、SPT-01 | 部分验证 | `test_spot_spec_has_no_pagination_and_uses_limit_precision`、`test_option_specs_all_bases_paginated_and_strike_from_symbol`、`test_repeated_cursor_fails_instead_of_partial_snapshot`、`test_spot_no_leverage_and_signed_json_bytes` | 规格更新后拒单、退市/非 Trading 状态拒开仓缺服务层断言；全部出站路径不调用借贷/划转尚无独立证明 |
| AC-03 · MKT-02/03、RSK-03 | 部分验证 | `test_ws_invalid_delta_requires_fresh_snapshot`、`test_ws_no_snapshot_and_reconnect_invalidate_delta`、`test_ws_crossed_book_invalidates_without_applying_partial_changes`、`test_private_disconnect_invalidates_before_backoff`、`test_missing_option_greeks_never_zero_delta` | 行情失效→暂停→重建→恢复的服务层组合场景未验；不可变决策行情快照引用未完整关联；worker 主链路目前为 REST 轮询 |
| AC-04 · STR-01/02 | 部分验证 | `test_schedule_100_duplicates_claim_once_and_skip_missed`、`test_enabled_template_schedule_fills_once_across_restart`、`test_global_physical_ids` | 时钟回拨与时区跨日去重、过期信号拒绝、意图所有必填字段正确填充缺专项断言 |
| AC-05 · STR-03、OPT-03 | 部分验证 | `test_enabled_template_schedule_fills_once_across_restart[option]`、`test_closed_batch_does_not_trigger_exit_of_new_inventory`、`test_extra_buy_inside_episode_keeps_original_hold_start`、费用预算/止盈回归；模板实现固定候选排序并记录规则 | 相同输入两次选择一致、无候选跳过、到期前退出缺专项测试；当前退出按策略/合约聚合库存，未实现逐笔买入批次退出 |
| AC-06 · STR-04、RSK-01 | 部分验证 | `test_all_commands_only_enqueue_once`、`test_commands_reject_invalid_scope_and_confirmation`、`test_csv_trace_joins_and_escapes_formulas` | 配置版本不可变、旧订单原版本、收紧限额重评挂单、策略停用撤单的完整链路未验；非法实盘配置缺专项测试 |
| AC-07 · RSK-02、SPT-02 | 部分验证 | `test_cash_reservation_competition`、`test_cash_freeze_overlap_requires_order_evidence`、`test_inventory_close_competition_and_strategy_ownership`、`test_inventory_freeze_overlap_requires_snapshot_evidence`、`test_costs_follow_fee_units_and_strategy` | 现金竞争测试使用同一策略的两个不同意图；双策略账户预算与策略子额度同时约束未验；零碎余额标识未实现；新增负债暂停/告警未验 |
| AC-08 · EXE-01/02 | 部分验证 | `test_unknown_never_retries_or_releases_missing_query`、`test_duplicate_id_is_not_rejection_evidence`、`test_ambiguous_writes_sent_once`、`test_explicit_write_rejection_is_not_unknown`、`test_ack_identity_mismatch_is_uncertain`、`test_recovery_orphan_reservation_proves_unsent_and_cannot_revive`、`test_prepared_unsent_recovery_releases_cash_units` | 查到同一活动订单后消除受理未知、合并最终成交的成功路径缺独立正例；人工 RESUME 不能释放预留缺组合断言 |
| AC-09 · EXE-01/02、LED-02 | 部分验证 | PREPARED/孤立预留恢复测试；`test_reconciliation_retains_offline_fill_and_watermark`、`test_start_baseline_takeover_trade_restart` | 已构造未发送状态并验证恢复；四个要求的崩溃时点均未完成真实子进程终止演练，尤其发送后未保存响应和部分成交后 |
| AC-10 · EXE-03、LED-01 | 部分验证 | `test_terminal_before_fill_preserves_reservation_and_no_double_count`、`test_late_fill_after_cancel_reclassifies_release_and_upgrades_full`、`test_terminal_and_fill_monotonic_state`、`test_private_events_dedupe_category_and_do_not_lose_other_category` | 成交先于撤单的独立排列、更正事件缺专项断言；SQL 成交去重键是全局 exec ID，与需求的账户/环境/产品分域仍有偏差 |
| AC-11 · EXE-04 | 部分验证 | `test_read_retries_bounded_and_limit_headers_recorded`、`test_long_server_cooldown_fails_without_retry_storm`、`test_transport_read_failure_retries_only_configured_attempts`、`test_exit_rebuild_preserves_deadline_quantity_and_floor`、`test_cancel_ack_without_terminal_preserves_held` | 本地 TTL 到期、停机错过 TTL、撤单查询超时缺专项断言；重建测试固定数量/期限/底价，未完整断言意图累计支出上限 |
| AC-12 · EXE-05、RSK-03 | 部分验证 | `test_lease_single_holder_and_no_auto_takeover`、`test_remote_adapter_cannot_use_paper_label_to_bypass_lease`；NAS 一把实际 advisory lock、当前持有者一条持续更新的审计行 | 测试为同进程两个 lease 对象及主动解锁；真实双进程竞争、数据库断连与失权在途请求尚未组合演练 |
| AC-13 · OPT-01/02 | 部分验证 | `test_option_sale_requires_reduce_only_and_one_way_position`、`test_option_close_credits_cash_and_cannot_short`、`test_inventory_close_competition_and_strategy_ownership`、`test_external_fill_is_recorded_but_never_owned`、`test_missing_position_locks_inventory_and_keeps_cost` | 两路完整平仓授权竞争、部分成交+未知卖单+撤单组合、外部改仓后自动卖出被拒及跨策略分摊未验 |
| AC-14 · RSK-05、OPT-03 | 部分验证 | `test_opposite_orders_cannot_offset`、`test_unquantified_delta_rejected`、`test_risk_uses_cash_and_two_sided_per_base_delta`、`test_missing_option_greeks_never_zero_delta` | 大负 Delta、已确认外部库存、仅一侧挂单成交、现货新单影响缺独立场景；权利金/集中度/价格边界拒绝与实际 Greeks 单位未验 |
| AC-15 · OPT-04、LED-01 | 部分验证 | `test_paper_settlement_checkpoint_and_ledger_replay`（零支付与正支付）、`test_settlement_evidence_idempotency_and_cost_release`；`test_deliveries.py` 116 项接口契约与 `test_settlement_reconciliation.py` 9 项对账测试 | 禁开仓窗口前后 TTL、撤单、晚到成交、告警余量的完整场景未验；真实交割解析已接入，尚无 Demo/Testnet 实际交割证据，零支付接口语义仍须实测 |
| AC-16 · LED-02/03、OPT-02 | 部分验证 | `test_external_fill_is_recorded_but_never_owned`、`test_external_cash_change_cannot_be_clean_recon`、`test_missing_position_locks_inventory_and_keeps_cost`、`test_reconciliation_retains_offline_fill_and_watermark` | 不撤销人工单缺出站断言；有审计的外部变化确认、库存导入及历史基线重建流程未实现完整闭环 |
| AC-17 · LED-04、RSK-03 | 部分验证 | `test_start_baseline_takeover_trade_restart`；`test_equity_is_own_cash_plus_option_value_without_double_counting`、`test_day_pnl_subtracts_external_flow`、`test_day_pnl_handles_outflow`、`test_adjusted_equity_removes_cumulative_flow`、`test_drawdown_is_peak_minus_current_and_never_negative`、`test_short_option_position_makes_valuation_incomplete` | 跨重启高点、换日锁定、旧/失效估值不能解暂停、首日不完整标记缺组合断言；风险资产精确日界线历史估值重建尚未实现 |
| AC-18 · RSK-04、OPS-02/03 | 部分验证 | `test_loss_pause_does_not_block_trusted_reduce`、`test_all_commands_only_enqueue_once`、`test_completed_command_preserves_actual_results` | 亏损触发→暂停→撤 bot 买单、数据库不可写、跨重启/换日保持锁定、RESUME 对账前置条件缺完整断言 |
| AC-19 · OPS-01/02 | 部分验证 | `test_status_is_environment_scoped_and_allowlisted`、`test_csv_trace_joins_and_escapes_formulas`、`test_console_requires_session_and_serves_local_assets`、`test_all_commands_only_enqueue_once`；实际浏览器管理页检查 | 未知订单、风控拒绝、所有金额币种与账户时效缺逐项展示断言；数据可追溯不等于所有页面内容已验收 |
| AC-20 · OPS-04/05、NFR-03 | 部分验证 | `test_delivery_retry_dedupe_and_resolution`、`test_independent_monitor_three_misses_one_alert_and_recovery`、`test_offsite_backup_staleness_is_independent_of_health`；Mac 实际任务和备份复制已验 | 未配置真实业务通知渠道；通知持续失败暂停新风险、各类故障告警上下文与时限、整机失联通知实际展示尚未验证 |
| AC-21 · SEC-01/02/03 | 部分验证 | `tests/test_web.py` 的认证/会话/CSRF/限流/脱敏用例；`tests/test_valuation_secrets.py` 的权限/属主/读取失败/脱敏用例；NAS 和本机数据库回环监听实测 | 缺代码/日志/镜像/导出的自动化全量泄漏扫描；实际固定出口与 API 权限未验；凭据轮换未做账户演练；API 凭据装载器未达到管理身份文件的同等软链接/描述符加固 |
| AC-22 · NFR-01/02/05 | 待验证 | 有容量与磁盘阈值配置、磁盘检查实现 | 没有容量负载、P95 延迟、CPU/内存、磁盘增长或磁盘不足暂停证据；数据保留清理尚未实现 |
| AC-23 · OPS-05、NFR-04、LED-02 | 部分验证 | `deploy/backup.sh` 流式加密；异机复制及独立库恢复已实测；恢复命令已记录在手册 | 尚无自动化恢复演练、备份后成交回填、只读服务启动及 RTO/RPO 测量；当前备份脚本仅覆盖 paper 数据库 |
| AC-24 · ENV-02、OPT-01/03/04 | 待验证 | REST 契约测试覆盖目录、钱包、期权参数、完整历史枚举和深度限制 | 没有真实凭据；期权权限、reduceOnly、模式、数量/报价/Greeks 单位、费用、成交/撤单/交割能力均未实测 |
| AC-25 · EXE-03、SPT-02、OPT-02、LED-04 | 部分验证 | `test_costs_follow_fee_units_and_strategy`、`test_inventory_close_competition_and_strategy_ownership`、`test_settlement_evidence_idempotency_and_cost_release`、`test_paper_settlement_checkpoint_and_ledger_replay`、重复权益防护单测 | 成本测试是单策略；同合约双策略多批买入/部分卖出未验；跨币费用组合及支付→估值→退出/结算全过程未串联验证 |
| AC-26 · NFR-06、STR-02 | 待验证 | `test_schedule_100_duplicates_claim_once_and_skip_missed`；`test_day_key_and_day_start_follow_configured_timezone` 验证了日界线转换 | 时钟前跳/回拨/偏差超限暂停未演练；`Deadline` 不受墙钟影响、退避稳定性、时间恢复后的资格检查缺专项断言 |
| AC-27 · RSK-02、OPT-03、ENV-02 | 部分验证 | `test_wallet_debt_not_double_counted_or_spendable`、`test_portfolio_unknown_holds_have_zero_spendable_cash`、现金预留竞争及冻结重叠证据测试 | 结算币现金不足但抵押品充足、运行中新负债→暂停/告警、无自动借贷/划转缺专项端到端场景 |
| AC-28 · STR-05 | 部分验证 | `test_next_book_depth_shared_cancel_and_checkpoint`、`test_invalid_book_and_non_touch_do_not_fill`、`test_unknown_registered_and_survives_json_restore`、`test_paper_guard_blocks_remote_writes` | 同一完整事件序列两次运行的确定性缺独立断言；故障实验/收益报告区分未实现；报告模型限制及费用版本缺完整断言 |

### 3.1 估值与凭据测试修正

`tests/test_valuation_secrets.py` 当前为 **28 项**。相较最初补测版本，本轮修正了原测试没有真正验证的行为：

- 重复权益测试显式输入已含期权价值的 `total_equity` / `exchange_equity`，再断言只按自有现金加一次期权价值计算。
- 仅多头估值遇到负数量，或正数量且 `side="Sell"`，均返回 `complete=False`；不再将忽略空头后得到的部分权益标记为完整。
- 利息即使没有借款本金也触发负债不完整。
- 属主测试模拟当前 UID 不匹配，实际触发 `load_credentials` 拒绝路径；读取失败测试实际触发 `PermissionError` 包装。
- 合成凭据不再作为测试帮助函数的默认参数，因此无需对全部测试豁免 Ruff `S107`。

这关闭了 G1/G9 所指的“缺少直接行为测试”，不代表 AC-17 或 AC-21 整体验收完成。测试未提供全量日志、镜像和导出的泄漏扫描。

### 3.2 两项曾被误判的实现

**估值恢复与锁定熔断不同。** `TradingService._value` 在本次估值不完整时写暂停并立即返回；只有本次估值完整、基线可用后才清除 `VALUATION_UNAVAILABLE`。`LOSS_CIRCUIT_BREAKER` 和 `DRAWDOWN_LIMIT` 属锁定原因，不随该清除动作解除。因此不能仅凭 `VALUATION_UNAVAILABLE` 是技术暂停，就认定系统会用旧净值解除熔断。仍需补组合回归测试。

**提现权限已参与身份门槛。** `TradingService._account_sync` 的 `identity_ok` 包含 `not key.withdraw_enabled`；失败会写 `ACCOUNT_UNTRUSTED` 暂停。当前缺口是该服务分支的专项断言与真实账户验证，不能表述为“只展示、不强制”。

### 3.3 主要实现边界

- worker 当前主运行链路使用 REST 轮询；WebSocket 适配器的契约测试不等于运行时主链路已集成。
- 自动退出按策略/合约的聚合库存管理。只有实际成交使库存从零变为正数时建立持有周期；追加买入保留原起点，完全平仓后的新买入重新计时。历史库存缺少周期字段时保持待核实，不猜测回填；重建共享原有效期、数量边界和底价。该实现不等价于逐笔购入批次管理。
- paper 结算是显式、持久化的合成事实。真实 Bybit 交割通过 delivery-record、DELIVERY 资金流水或零支付的独立到期平仓证据接入，核对先前可信现金快照及完整持仓后独立写 SETTLEMENT 和现金流。证据缺失或冲突时保留成本；尚无真实账户验证记录。
- 首次基线须显式建立。已有基线与历史高点需保留；缺失历史不能靠重新初始化消除差异。风险资产的精确日界线历史重建未实现，目前仅有保守的纯现金不变场景重建。
- 原始行情和审计/交易数据的保留配置存在，但自动清理未实现。不能从配置值推断保留任务已经运行。
- 没有外部库存确认/导入和基线重建的完整审计操作闭环。
- `clock.local_day` / `local_day_start_utc` 已由估值日界线测试间接断言；时钟跳变、超限偏差和单调截止时间语义仍未验。不能把“没有直接 import”写成“模块零覆盖”。

## 4. 已核验的部署事实

以下为实际 NAS/Mac 操作结果，与模拟测试证据分列。未执行 NAS 策略启用、首次基线初始化、接管确认或实盘启用。

| 项目 | 2026-11-01 核验结果 |
|---|---|
| 软件版本 | `bybitbot 0.1.0`，NAS Python 3.12.15，PostgreSQL 16 |
| NAS 当前应用镜像 | `474fbae0c7c501d3dd7fca2ec996c1536bc3c3c2c9b1549c87215eb5dcb7151f`，worker/web 均运行此镜像，发布标签 `localhost/bybitbot:dogfood-20261101` |
| 上一应用镜像 | `c1d89f9541d9b86f8b67ee173933b73f86808c48f135db95e1a55475937de049`，标签 `localhost/bybitbot:pre-dogfood-20261101`；更早的 `pre-valuation-20261031` 标签保留 |
| 更早回滚镜像 | `710a347cad5135f56c16d6b77c84034d2cbc16cc091d43277f01f68c712a3003`，标签 `localhost/bybitbot:rollback-20261031` |
| 依赖运行时镜像 | `0590a0bde8c19590e5f6f3b5459a111c8541c45cb9ac05049e55b038f15b0dbe`，标签 `localhost/bybitbot-runtime:0.1.0` |
| NAS Python 基础镜像 | `3c4d812a6efe003770aafbdba65f9c1960ba8234ab23a133934205a70bdcda77`，标签 `localhost/artex-runtime:python3.12` |
| 源码一致性 | 新镜像中 53 个应用、页面资源、迁移与配置示例文件逐一 SHA-256 与本地相同 |
| 服务 | PostgreSQL、worker、web 和备份 timer 均 active；rootless Quadlet，用户 linger 已开启 |
| 配置检查 | 部署镜像 CLI `check` 成功：paper，两策略 false，`live_allowed=false` |
| 运行状态 | `PAUSED_OPEN` / `NEEDS_CONFIRMATION`；USDT 1000；`baseline_available=false`；`valuation_complete=true`；`last_error` 为空；原 `VALUATION_UNAVAILABLE`、`NOTIFICATION_FAILURE`、`CONFIG_CHANGED` 暂停仍在，未代为解除 |
| 策略/成交 | `btc-daily-spot=false`、`eth-call-weekly=false`；SQL 成交总数 0 |
| 租约 | `pg_locks` 实际 granted advisory lock 为 1；最新持有者只有一条审计行且心跳持续更新。历史租约表中共 161 条 `released_at IS NULL` 行，其中当前持有者 1 条、旧版遗留 160 条；这些历史行不代表 161 个持锁执行者 |
| 数据库迁移 | Alembic head `8a4d17c2f905`，新增库存持有周期字段；本地 `alembic check` 无新操作；NAS 旧无交易 paper 检查点已保留余额迁移至 `bybit-fee-v1`，旧检查点另存审计副本 |
| 网络入口 | NAS `ss -ltn` 确认 PostgreSQL 仅 `127.0.0.1:55432`、web 仅 `127.0.0.1:18080`；HTTPS 由 tailnet 入口提供；本地测试数据库也已改为回环端口 |
| HTTPS 健康 | `https://nicholas-4.tail068b64.ts.net/health` 返回 `{"status":"ok"}` |
| 管理页 | 已核验 HTTPS 登录、未登录 401、Secure/HttpOnly/SameSite=Strict cookie 与退出；本地浏览器已检查桌面和移动布局；写保护另有合成测试 |
| 加密异机备份 | `pg_dump -Fc` 流式 GPG 加密，实际复制到 Mac；已解密恢复至独立库，head、2 策略、2 运行态、0 成交符合源数据；演练库及明文 dump 已清理 |
| Mac 独立任务 | launchd 心跳 30 秒、备份复制 1800 秒，两任务最近 exit 0；心跳已重复运行，`healthy=true`、失败 0、备份未逾期；实际加密文件存在且 0600 |
| 监控限制 | 未做真实三次失联通知展示演练；Mac 睡眠/离线期间不监控。`/health` 另有 120 秒心跳有效期，不能声称 worker 停止后固定 90 秒收到通知 |
| 通知渠道 | `ops.alert_channels=[]`，无真实远端业务告警；当前业务告警只落库 |
| 真实 Bybit 能力 | 未提供交易凭据；没有真实 Demo/Testnet 或实盘发单；paper capability=true 只描述模拟能力 |
| GitHub | 已通过 NAS 上的 `NicholasPython` 认证上传至私有仓库 [NicholasPython/bybit](https://github.com/NicholasPython/bybit)，默认分支 `main`；首次上传提交 `4dab9f43476d07b6bc531845404096c9ce9f1cd0` 与本地一致；GitHub Actions 已触发，执行结果以 [Actions 记录](https://github.com/NicholasPython/bybit/actions) 为准 |

NAS 使用已有本地 Python 基础层构建成功。标准 `Containerfile` 在 Mac 的构建因 Docker Hub 基础镜像 TLS 握手超时失败，该路径不能列为已验证。

备份恢复已人工成功且手册提供可重复命令；仍未自动化、未测恢复只读服务的完整耗时、未回填备份后成交，也没有 RTO ≤ 2h / RPO ≤ 24h 的测量记录。

## 5. 剩余验收工作

以下为未完成的阶段条件，列出它们不构成账户启用或自动执行授权。

| 类别 | 必须取得的证据/实现 |
|---|---|
| 本地拒绝路径 | 非法配置、各风控限额、禁开仓窗口、过期信号、费用余量与权限失败的参数化拒绝证据 |
| 服务层组合 | 配置版本、限额收紧、暂停/撤单/可信减仓、日界线/高点、失效估值与人工恢复的组合证据 |
| 故障与互斥 | 四个崩溃点的子进程演练、双进程竞争、真实数据库断连与未知在途请求处理 |
| 交易与账本 | 多策略同合约成本、外部库存审计导入、真实账户交割口径核验及支付→估值→退出/结算完整核对 |
| 时钟 | 前跳/回拨/偏差超限、调度不重复补做、Deadline 与退避不受墙钟影响 |
| 非实盘能力 | 使用受限 Demo/Testnet 凭据核对身份、权限、模式、期权单位、reduceOnly、成交/撤单和结算能力；不支持项保持未通过 |
| 运维 | 容量/P95/磁盘增长、保留清理、磁盘不足暂停、真实通知送达与失败、完整备份恢复和 RTO/RPO |
| 安全 | 自动化密钥泄漏扫描、API 凭据文件进一步加固、固定出口核对、保持在途可追踪的实际凭据轮换 |
| 远端 CI | 仓库已创建并推送，工作流已触发；CI 通过不替代真实账户和阶段验收 |

提现权限拒绝门槛已有实现；待补的是测试和实际权限验证。失效估值恢复路径已有前置条件；待补的是组合断言，未确认存在语义缺陷。

## 6. 测试分布

| 文件 | 收集项数 | 主要覆盖 |
|---|---|---|
| `tests/test_adapters.py` | 71 | REST/WS 契约、签名、端点、分页、身份、现金语义、限流与重试 |
| `tests/test_web.py` | 42 | 认证、会话、CSRF、命令幂等、脱敏、CSV、管理身份文件 |
| `tests/test_execution.py` | 19 | UNKNOWN、终态/晚到成交、预留、恢复、租约、对账、结算 |
| `tests/test_core.py` | 15 | Delta 区间、现金/库存竞争、成本费用、调度去重、物理单号 |
| `tests/test_service.py` | 12 | paper 基线/接管/重启、模板调度、退出周期与重建、费用预算/止盈、旧费用检查点迁移、结算重放 |
| `tests/test_deliveries.py` | 116 | 真实交割接口契约、正/零支付、独立证据、分页、时间、币种、冲突及普通卖出排除 |
| `tests/test_settlement_reconciliation.py` | 9 | 交割现金及持仓核对、成本释放、重放、延迟证据恢复、现金漂移拒绝 |
| `tests/test_fees.py` | 13 | 官方费用公式、额外费用、数量测算、指数失效保守封顶、模拟撮合与检查点 |
| `tests/test_paper.py` | 4 | 深度共享、未触价、检查点、未知结果、平仓 |
| `tests/test_deploy_ops.py` | 4 | 失联计数、备份年龄、监控安装路径及私有权限 |
| `tests/test_valuation_secrets.py` | 28 | 权益、损益、回撤、日界线、空头拒绝、凭据权限/属主/读取失败/脱敏 |
| `tests/test_ops.py` | 1 | 告警投递、重试、去重、恢复 |
| `tests/test_cli.py` | 1 | 管理身份生成、权限与拒绝覆盖 |
| **合计** | **335** | 另有 Node 页面提示回归 4 项 |

本文未以 import 次数代替代码覆盖率，也未测得行/分支覆盖率百分比。各 AC 的剩余差距基于具体输入与断言是否存在；测试通过和真实部署正常均不自动赋予实盘发单资格。
