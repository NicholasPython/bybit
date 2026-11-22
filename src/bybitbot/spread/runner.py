"""配对持久化执行器。先提交发送标记，再调用模拟交易所；未知结果只校准不重发。

阶段二只允许隔离的 paper 账户。交易所写请求、真实保证金、资金费账单与 WS
常驻消费尚无验收证据，因此远程适配器不能通过本入口获得发单资格。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from decimal import Decimal

from sqlalchemy import select, text

from bybitbot.adapters.base import OrderRejected, OrderRequest
from bybitbot.adapters.paper import PaperAdapter
from bybitbot.clock import now_utc
from bybitbot.execution.executor import DUPLICATE_ORDER_LINK_RETCODES, ExecutionAuthorityError
from bybitbot.execution.lease import LeaseState
from bybitbot.settings.models import OrderSide, OrderType, TimeInForce
from bybitbot.spread.accounting import TERMINAL_ORDERS, TERMINAL_PAIRS, pair_facts
from bybitbot.spread.execution import PairSignal, order_link_id
from bybitbot.spread.models import (
    SpreadExecution,
    SpreadLegOrder,
    SpreadPairConfig,
    SpreadPairTrade,
)
from bybitbot.spread.reconciliation import SpreadReconciler
from bybitbot.spread.risk import PairRiskContext, PairRiskLimits, evaluate_open
from bybitbot.spread.states import equal_notional_qty, quantize_qty, slippage_bps

ZERO = Decimal("0")


def margin_rate(account):
    value = account.raw.get("account_mm_rate")
    if value is None:
        raise ExecutionAuthorityError("维持保证金率不可用")
    rate = Decimal(str(value))
    if not rate.is_finite() or rate < 0:
        raise ExecutionAuthorityError("维持保证金率必须为非负有限数")
    return rate


def encode_signal(signal):
    return json.loads(json.dumps(asdict(signal), default=str))


class SpreadExecutor:
    def __init__(self, *, adapter, sessionmaker, environment, account_uid, settings, lease=None):
        if environment != "paper" or not isinstance(adapter, PaperAdapter):
            raise ExecutionAuthorityError("阶段二套利执行器仅允许隔离 paper 适配器")
        self.adapter, self.sessionmaker = adapter, sessionmaker
        self.environment, self.account_uid = environment, account_uid
        self.settings, self.lease = settings, lease
        self.reconciler = SpreadReconciler(
            adapter=adapter,
            sessionmaker=sessionmaker,
            environment=environment,
            account_uid=account_uid,
            record_interval_seconds=settings.reconciliation_interval_seconds,
        )
        self._mutex = asyncio.Lock()
        self.recovering = True
        self.last_result = None

    async def _authority(self):
        if self.adapter.environment != self.environment:
            raise ExecutionAuthorityError("套利适配器环境不匹配")
        if self.lease is not None:
            if (
                self.lease.environment != self.environment
                or self.lease.account_uid != self.account_uid
            ):
                raise ExecutionAuthorityError("套利执行租约不属于本账户")
            if (await self.lease.heartbeat()).state is not LeaseState.ELIGIBLE:
                raise ExecutionAuthorityError("套利执行需要已确认的执行者租约")
        account = await self.adapter.get_account()
        if account.account_uid != self.account_uid or account.has_debt:
            raise ExecutionAuthorityError("套利账户身份不匹配或存在负债")

    async def _lock(self, session):
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": f"{self.environment}:spread"},
        )

    async def configure(self):
        async with self.sessionmaker() as session:
            await self._lock(session)
            for config in self.settings.pairs:
                row = await session.scalar(
                    select(SpreadPairConfig).where(
                        SpreadPairConfig.environment == self.environment,
                        SpreadPairConfig.pair_key == config.pair_key,
                    )
                )
                if row is None:
                    row = SpreadPairConfig(environment=self.environment, pair_key=config.pair_key)
                    session.add(row)
                for key in (
                    "symbol_a",
                    "symbol_b",
                    "hedge_ratio",
                    "z_entry",
                    "z_exit",
                    "z_stop",
                    "passive_timeout_ms",
                    "recovery_window_ms",
                    "max_slippage_bps",
                    "enabled",
                ):
                    setattr(row, key, getattr(config, key))
                row.max_notional = config.max_notional.value
                row.max_unhedged_notional = config.max_unhedged_notional.value
                row.status = "ACTIVE" if config.enabled and self.settings.enabled else "DISABLED"
            await session.commit()

    async def recover(self):
        """启动时只读校准。领取发送记录不能因重启变回可发送状态。"""
        self.recovering = True
        await self.configure()
        async with self.sessionmaker() as session:
            await self._lock(session)
            pairs = list(
                await session.scalars(
                    select(SpreadPairTrade).where(SpreadPairTrade.environment == self.environment)
                )
            )
            for pair in pairs:
                if pair.state not in TERMINAL_PAIRS:
                    pair.context = {**pair.context, "restart_cleanup": True}
            await session.commit()
        self.last_result = await self.reconciler.reconcile()
        return self.last_result

    async def open_pair(self, signal: PairSignal):
        async with self._mutex:
            await self._authority()
            if not self.settings.enabled or self.recovering:
                raise ExecutionAuthorityError("套利关闭或尚未完成重启校准")
            # 幂等重入先核验完整载荷；不因同名标识重复发单。
            async with self.sessionmaker() as session:
                existing = await session.scalar(
                    select(SpreadPairTrade).where(
                        SpreadPairTrade.environment == self.environment,
                        SpreadPairTrade.pair_trade_id == signal.pair_trade_id,
                    )
                )
                if existing is not None:
                    if existing.account_uid != self.account_uid or existing.context.get(
                        "signal"
                    ) != encode_signal(signal):
                        raise ValueError("配对编号已被不同信号占用")
                    return existing
            self.last_result = await self.reconciler.reconcile()
            if not self.last_result.allows_resume:
                raise ExecutionAuthorityError("套利账户校准存在差异")
            config = next(
                (
                    item
                    for item in self.settings.pairs
                    if item.pair_key == signal.pair_key and item.enabled
                ),
                None,
            )
            if config is None:
                raise ValueError("配对未配置或未启用")
            quotes, specs = await self._inputs({signal.symbol_a, signal.symbol_b})
            self._validate_signal(signal, config, quotes, specs)
            account = await self.adapter.get_account()
            async with self.sessionmaker() as session:
                await self._lock(session)
                # 不允许共享合约：单向持仓下无法对不同配对分别 reduceOnly。
                active = list(
                    await session.scalars(
                        select(SpreadPairTrade).where(
                            SpreadPairTrade.environment == self.environment,
                            SpreadPairTrade.closed_at.is_(None),
                        )
                    )
                )
                if len(active) >= self.settings.max_concurrent_pairs:
                    raise ExecutionAuthorityError("同时持有配对数达到上限")
                if any(
                    {p.symbol_a, p.symbol_b} & {signal.symbol_a, signal.symbol_b} for p in active
                ):
                    raise ExecutionAuthorityError("阶段二不允许活跃配对共享合约")
                # notional 是每腿预算；风控与预留按双腿总名义计算。
                reserved = sum((2 * row.target_notional for row in active), ZERO)
                decision = evaluate_open(
                    limits=PairRiskLimits(
                        max_pair_equity_ratio=self.settings.max_pair_equity_ratio,
                        max_account_leverage=self.settings.max_account_leverage,
                    ),
                    context=PairRiskContext(
                        equity=account.total_equity or ZERO,
                        account_notional=reserved,
                        mm_rate=margin_rate(account),
                    ),
                    pair_notional=2 * signal.notional,
                    pair_notional_cap=config.max_notional.value,
                )
                if not decision.allowed:
                    raise ExecutionAuthorityError(decision.reason_text)
                config_row = await session.scalar(
                    select(SpreadPairConfig).where(
                        SpreadPairConfig.environment == self.environment,
                        SpreadPairConfig.pair_key == signal.pair_key,
                    )
                )
                pair = SpreadPairTrade(
                    environment=self.environment,
                    account_uid=self.account_uid,
                    pair_trade_id=signal.pair_trade_id,
                    pair_key=signal.pair_key,
                    config_id=config_row.id,
                    state="OPENING",
                    direction=signal.direction,
                    symbol_a=signal.symbol_a,
                    symbol_b=signal.symbol_b,
                    side_a=signal.side_a,
                    side_b=signal.side_b,
                    target_notional=signal.notional,
                    opened_at=now_utc(),
                    context={
                        "signal": encode_signal(signal),
                        "first_unhedged_ms": None,
                        "shortfall_ms": None,
                        "restart_cleanup": False,
                    },
                )
                session.add(pair)
                await session.flush()
                self._queue(
                    session,
                    pair,
                    [],
                    leg="A",
                    role="PASSIVE",
                    qty=quantize_qty(signal.notional / signal.passive_price, signal.qty_step_a),
                    price=signal.passive_price,
                )
                await session.commit()
            return pair

    async def request_close(self, pair_trade_id, *, reason="人工平仓"):
        async with self._mutex:
            await self._authority()
            async with self.sessionmaker() as session:
                await self._lock(session)
                pair = await session.scalar(
                    select(SpreadPairTrade)
                    .where(
                        SpreadPairTrade.environment == self.environment,
                        SpreadPairTrade.account_uid == self.account_uid,
                        SpreadPairTrade.pair_trade_id == pair_trade_id,
                    )
                    .with_for_update()
                )
                if pair is None:
                    raise ValueError("配对不属于当前账户")
                if pair.state not in TERMINAL_PAIRS:
                    pair.state = "CLOSING"
                pair.last_error = reason
                pair.context = {**pair.context, "close_requested": True}
                await session.commit()

    async def tick(self, *, now_ms=None, paused=False):
        async with self._mutex:
            stamp = now_ms if now_ms is not None else int(now_utc().timestamp() * 1000)
            # 小批次推进，避免 IOC 拒单在一个 tick 中产生无界发单循环。
            for _ in range(4):
                self.last_result = await self.reconciler.reconcile()
                if not self.last_result.allows_resume:
                    return self.last_result
                await self._authority()
                async with self.sessionmaker() as session:
                    pairs = list(
                        await session.scalars(
                            select(SpreadPairTrade).where(
                                SpreadPairTrade.environment == self.environment,
                                SpreadPairTrade.closed_at.is_(None),
                            )
                        )
                    )
                symbols = {symbol for pair in pairs for symbol in (pair.symbol_a, pair.symbol_b)}
                quotes, specs = await self._inputs(symbols)
                account = await self.adapter.get_account()
                mm_rate = margin_rate(account)
                if not mm_rate.is_finite():
                    raise ExecutionAuthorityError("缺少维持保证金率")
                sends, cancels, progressed = await self._plan(stamp, quotes, specs, paused, mm_rate)
                if not sends and not cancels and not progressed:
                    self.recovering = False
                    break
                for order_id in cancels:
                    await self._cancel(order_id)
                for order_id in sends:
                    await self._send(order_id)
            self.last_result = await self.reconciler.reconcile()
            return self.last_result

    async def _plan(self, stamp, quotes, specs, paused, mm_rate):
        sends, cancels, progressed = [], [], False
        async with self.sessionmaker() as session:
            await self._lock(session)
            pairs = list(
                await session.scalars(
                    select(SpreadPairTrade)
                    .where(
                        SpreadPairTrade.environment == self.environment,
                        SpreadPairTrade.closed_at.is_(None),
                    )
                    .with_for_update()
                )
            )
            all_orders = list(
                await session.scalars(
                    select(SpreadLegOrder).where(SpreadLegOrder.environment == self.environment)
                )
            )
            fills = list(
                await session.scalars(
                    select(SpreadExecution).where(SpreadExecution.environment == self.environment)
                )
            )
            for pair in pairs:
                if pair.account_uid != self.account_uid:
                    raise ExecutionAuthorityError("配对账户不匹配")
                orders = [o for o in all_orders if o.pair_trade_id == pair.id]
                facts = pair_facts(orders, fills)
                signal = pair.context["signal"]
                context = dict(pair.context)
                live_entry = [
                    o for o in orders if o.role != "FLATTEN" and o.status not in TERMINAL_ORDERS
                ]
                live_close = [
                    o for o in orders if o.role == "FLATTEN" and o.status not in TERMINAL_ORDERS
                ]
                remaining = {leg: facts.remaining(leg) for leg in ("A", "B")}
                if mm_rate > Decimal("0.75") and pair.state not in TERMINAL_PAIRS:
                    pair.state, pair.last_error = "CLOSING", "维持保证金率触发强制减仓"
                    context["close_requested"] = True
                entry_a = next((o for o in orders if o.role == "PASSIVE"), None)
                deadline = int(pair.opened_at.timestamp() * 1000) + int(
                    signal["passive_timeout_ms"]
                )
                cleanup = context.get("restart_cleanup") or paused or not self.settings.enabled
                if (
                    entry_a
                    and entry_a.status not in TERMINAL_ORDERS
                    and (stamp >= deadline or cleanup)
                ):
                    cancels.append(entry_a.id)
                if context.get("restart_cleanup") and not live_entry:
                    context["restart_cleanup"] = False
                closing = context.get("close_requested") or pair.state in TERMINAL_PAIRS | {
                    "CLOSING"
                }
                if closing:
                    cancels.extend(o.id for o in live_entry)
                    if not live_entry:
                        for leg in ("A", "B"):
                            if remaining[leg] > 0 and not any(o.leg == leg for o in live_close):
                                symbol = pair.symbol_a if leg == "A" else pair.symbol_b
                                side = pair.side_a if leg == "A" else pair.side_b
                                ticker = quotes[symbol]
                                price = ticker.bid_price if side == "Buy" else ticker.ask_price
                                self._queue(
                                    session,
                                    pair,
                                    orders,
                                    leg=leg,
                                    role="FLATTEN",
                                    qty=remaining[leg],
                                    price=price,
                                )
                        if not any(remaining.values()) and not live_close:
                            if pair.state == "CLOSING":
                                pair.state = "CLOSED"
                            pair.closed_at = now_utc()
                    pair.context = context
                    continue
                qty_b = equal_notional_qty(
                    filled_notional_a=facts.entry_value["A"],
                    price_b=Decimal(signal["active_price"]),
                    qty_step_b=Decimal(signal["qty_step_b"]),
                )
                residual = qty_b - facts.opened["B"]
                live_b = [o for o in live_entry if o.leg == "B"]
                if residual > 0 and not live_b:
                    previous = [o for o in orders if o.leg == "B" and o.role != "FLATTEN"]
                    ticker = quotes[pair.symbol_b]
                    price = ticker.ask_price if pair.side_b == "Buy" else ticker.bid_price
                    within_slippage = slippage_bps(
                        Decimal(signal["active_price"]), price
                    ) <= Decimal(signal["max_slippage_bps"])
                    if context.get("shortfall_ms") is None:
                        context["shortfall_ms"] = stamp
                    in_window = stamp - context["shortfall_ms"] <= int(signal["recovery_window_ms"])
                    if within_slippage and in_window:
                        self._queue(
                            session,
                            pair,
                            orders,
                            leg="B",
                            role="RECOVERY" if previous else "ACTIVE",
                            qty=residual,
                            price=price,
                        )
                    else:
                        pair.state, pair.last_error = "ERROR_UNHEDGED", "对冲缺口超时或滑点越界"
                        context["close_requested"] = True
                elif residual <= 0:
                    context["shortfall_ms"] = None
                unhedged = abs(
                    remaining["A"] * quotes[pair.symbol_a].mark_price
                    - remaining["B"] * quotes[pair.symbol_b].mark_price
                )
                if unhedged > Decimal(signal["max_unhedged_notional"]):
                    if context.get("first_unhedged_ms") is None:
                        context["first_unhedged_ms"] = stamp
                    if stamp - context["first_unhedged_ms"] >= int(signal["unhedged_grace_ms"]):
                        pair.state, pair.last_error = "ERROR_UNHEDGED", "单腿裸敞口持续超限"
                        context["close_requested"] = True
                else:
                    context["first_unhedged_ms"] = None
                if not live_entry and not any(o.status == "PENDING" for o in orders):
                    if not facts.opened["A"] and not facts.opened["B"]:
                        pair.state, pair.closed_at = "ABORTED", now_utc()
                    elif (
                        facts.opened["A"] > 0
                        and facts.opened["B"] > 0
                        and residual <= 0
                        and unhedged <= Decimal(signal["max_unhedged_notional"])
                    ):
                        pair.state = "HEDGED"
                        pair.hedged_at = pair.hedged_at or now_utc()
                    elif qty_b == 0 and facts.opened["A"] > 0:
                        pair.state, pair.last_error = "ERROR_UNHEDGED", "成交不足以满足对冲数量步长"
                        context["close_requested"] = True
                if context.get("close_requested") and not pair.context.get("close_requested"):
                    progressed = True
                pair.context = context
            await session.flush()
            queued = list(
                await session.scalars(
                    select(SpreadLegOrder).where(
                        SpreadLegOrder.environment == self.environment,
                        SpreadLegOrder.status == "PENDING",
                        SpreadLegOrder.submitted_at.is_(None),
                    )
                )
            )
            # 计划新产生的平仓/对冲在下一轮校准后领取。被取消的被动单不可发送。
            for row in queued:
                pair = next(p for p in pairs if p.id == row.pair_trade_id)
                if row.role != "FLATTEN" and pair.context.get("close_requested"):
                    cancels.append(row.id)
                elif row.id not in cancels:
                    sends.append(row.id)
            await session.commit()
        return sends, list(dict.fromkeys(cancels)), progressed

    def _queue(self, session, pair, orders, *, leg, role, qty, price):
        if not qty.is_finite() or qty <= 0 or not price.is_finite() or price <= 0:
            raise ValueError("套利动作数量与保护价必须为正的有限数")
        sequence = len([o for o in orders if o.leg == leg])
        side = pair.side_a if leg == "A" else pair.side_b
        if role == "FLATTEN":
            side = "Sell" if side == "Buy" else "Buy"
        row = SpreadLegOrder(
            environment=self.environment,
            pair_trade_id=pair.id,
            leg=leg,
            role=role,
            order_link_id=order_link_id(
                pair_trade_id=pair.pair_trade_id, leg=leg, sequence=sequence
            ),
            symbol=pair.symbol_a if leg == "A" else pair.symbol_b,
            side=side,
            price=price,
            qty=qty,
            status="PENDING",
            sync_state="NONE",
            time_in_force="PostOnly" if role == "PASSIVE" else "IOC",
        )
        session.add(row)
        orders.append(row)
        return row

    async def _record_write(self, session, order):
        pair = await session.scalar(
            select(SpreadPairTrade)
            .where(
                SpreadPairTrade.id == order.pair_trade_id,
                SpreadPairTrade.environment == self.environment,
                SpreadPairTrade.account_uid == self.account_uid,
            )
            .with_for_update()
        )
        if pair is None:
            raise ExecutionAuthorityError("套利物理订单没有可信账户归属")
        stamp = int(now_utc().timestamp() * 1000)
        events = [
            int(value)
            for value in pair.context.get("write_events_ms", [])
            if int(value) > stamp - 10_000
        ]
        if len(events) >= 15:
            raise ExecutionAuthorityError("同配对 10 秒内发单/撤单达到 15 次，等待冷却")
        pair.context = {**pair.context, "write_events_ms": [*events, stamp]}

    async def _send(self, order_id):
        await self._authority()
        async with self.sessionmaker() as session:
            await self._lock(session)
            row = await session.scalar(
                select(SpreadLegOrder)
                .where(
                    SpreadLegOrder.id == order_id, SpreadLegOrder.environment == self.environment
                )
                .with_for_update()
            )
            if row.status != "PENDING" or row.submitted_at is not None:
                return
            await self._record_write(session, row)
            row.submitted_at, row.sync_state, row.status = now_utc(), "PENDING", "UNKNOWN"
            request = OrderRequest(
                category="linear",
                symbol=row.symbol,
                side=OrderSide(row.side),
                order_type=OrderType.LIMIT,
                qty=row.qty,
                price=row.price,
                time_in_force=TimeInForce(row.time_in_force),
                order_link_id=row.order_link_id,
                reduce_only=row.role == "FLATTEN",
                position_idx=0,
            )
            await session.commit()
        try:
            await self._authority()
            ack = await self.adapter.create_order(request)
            if ack.order_link_id != request.order_link_id:
                raise ValueError("套利回执编号不匹配")
        except OrderRejected as exc:
            if exc.ret_code in DUPLICATE_ORDER_LINK_RETCODES:
                await self._unknown(order_id)
            else:
                async with self.sessionmaker() as session:
                    row = await session.get(SpreadLegOrder, order_id)
                    row.status, row.sync_state = "REJECTED", "CONFIRMED"
                    row.last_error = "交易所明确拒绝受理"
                    await session.commit()
            return
        except asyncio.CancelledError:
            await asyncio.shield(self._unknown(order_id))
            raise
        except Exception:  # noqa: BLE001 -- 标记已发送且未知，不能更换编号重试。
            await self._unknown(order_id)
            return
        async with self.sessionmaker() as session:
            row = await session.get(SpreadLegOrder, order_id)
            row.exchange_order_id = ack.order_id
            # 受理回执不等于成交事实；下一轮三维校准前保持 pending。
            await session.commit()

    async def _unknown(self, order_id):
        async with self.sessionmaker() as session:
            row = await session.get(SpreadLegOrder, order_id)
            row.status, row.sync_state = "UNKNOWN", "UNCERTAIN"
            row.last_error = "发送结果待交易事实核实，禁止重发"
            await session.commit()

    async def _cancel(self, order_id):
        await self._authority()
        async with self.sessionmaker() as session:
            await self._lock(session)
            row = await session.scalar(
                select(SpreadLegOrder)
                .where(
                    SpreadLegOrder.id == order_id, SpreadLegOrder.environment == self.environment
                )
                .with_for_update()
            )
            if row.status in TERMINAL_ORDERS:
                return
            if row.submitted_at is None:
                row.status, row.sync_state = "CANCELED", "CONFIRMED"
                await session.commit()
                return
            await self._record_write(session, row)
            symbol, link = row.symbol, row.order_link_id
            row.sync_state = "PENDING"
            await session.commit()
        try:
            await self._authority()
            await self.adapter.cancel_order("linear", symbol, order_link_id=link)
        except asyncio.CancelledError:
            await asyncio.shield(self._unknown(order_id))
            raise
        except Exception:  # noqa: BLE001 -- 撤单回执缺失也必须重新查成交与订单。
            await self._unknown(order_id)

    async def _inputs(self, symbols):
        specs = {spec.symbol: spec for spec in await self.adapter.get_instruments("linear")}
        quotes = {}
        for symbol in symbols:
            spec = specs.get(symbol)
            ticker = await self.adapter.get_ticker("linear", symbol)
            stamp = ticker.source_ts or ticker.received_at
            values = (ticker.bid_price, ticker.ask_price, ticker.mark_price)
            if (
                spec is None
                or not spec.is_tradable
                or spec.settle_coin != "USDT"
                or not spec.tick_size
                or not spec.qty_step
                or any(value is None or not value.is_finite() or value <= 0 for value in values)
                or ticker.bid_price > ticker.ask_price
                or stamp is None
                or abs((now_utc() - stamp).total_seconds()) > 15
            ):
                raise ExecutionAuthorityError("套利规格、行情或估值数据不完整/过期")
            quotes[symbol] = ticker
        return quotes, specs

    @staticmethod
    def _validate_signal(signal, config, quotes, specs):
        expected_side = {"LONG_A_SHORT_B": ("Buy", "Sell"), "SHORT_A_LONG_B": ("Sell", "Buy")}
        if (
            signal.symbol_a != config.symbol_a
            or signal.symbol_b != config.symbol_b
            or expected_side.get(signal.direction) != (signal.side_a, signal.side_b)
        ):
            raise ValueError("套利信号方向或合约不匹配")
        if not signal.pair_trade_id or len(signal.pair_trade_id) > 28:
            raise ValueError("配对编号必须为 1 至 28 字符")
        order_link_id(pair_trade_id=signal.pair_trade_id, leg="A", sequence=0)
        for name in ("notional", "passive_price", "active_price", "qty_step_a", "qty_step_b"):
            value = getattr(signal, name)
            if not value.is_finite() or value <= 0:
                raise ValueError("套利信号数量与价格必须为正的有限数")
        if (
            signal.qty_step_a != specs[signal.symbol_a].qty_step
            or signal.qty_step_b != specs[signal.symbol_b].qty_step
            or signal.passive_timeout_ms != config.passive_timeout_ms
            or signal.recovery_window_ms != config.recovery_window_ms
            or signal.max_slippage_bps != config.max_slippage_bps
            or signal.max_unhedged_notional != config.max_unhedged_notional.value
            or signal.unhedged_grace_ms != 5000
        ):
            raise ValueError("套利信号不能放宽冻结配置约束")
        for symbol, price in (
            (signal.symbol_a, signal.passive_price),
            (signal.symbol_b, signal.active_price),
        ):
            spec = specs[symbol]
            if price % spec.tick_size:
                raise ValueError("套利保护价不符合价格步长")
            qty = quantize_qty(signal.notional / price, spec.qty_step)
            if (
                qty <= 0
                or spec.min_order_qty
                and qty < spec.min_order_qty
                or spec.max_order_qty
                and qty > spec.max_order_qty
                or spec.min_order_amt
                and qty * price < spec.min_order_amt
            ):
                raise ValueError("套利数量不符合合约最小/最大订单限制")
            ticker = quotes[symbol]
            reference = (
                ticker.ask_price
                if symbol == signal.symbol_b and signal.side_b == "Buy"
                else ticker.bid_price
            )
            if slippage_bps(reference, price) > config.max_slippage_bps:
                raise ValueError("套利信号保护价偏离当前盘口")
