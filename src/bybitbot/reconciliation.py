"""恢复交易事实和预留；缺少证据时保留风险占用与暂停（LED-02/03）。"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import func, select

from bybitbot.adapters.base import InstrumentSpec, PositionView, TradingAdapter
from bybitbot.clock import now_utc
from bybitbot.db.models import (
    AccountSnapshot,
    CashFlow,
    Execution,
    Instrument,
    Inventory,
    Order,
    Position,
    ReconciliationRun,
    Reservation,
    TradeIntent,
)
from bybitbot.execution.states import (
    CANCELED,
    PREPARED,
    SYNC_CONFIRMED,
    SYNC_NONE,
    SYNC_UNCERTAIN,
    TERMINAL_STATES,
    UNKNOWN,
)
from bybitbot.ledger import LedgerWriter
from bybitbot.risk.reservation import ReservationManager
from bybitbot.settings.models import ReservationStatus

ZERO = Decimal("0")
TOLERANCE = Decimal("1e-12")


@dataclass
class Difference:
    kind: str
    detail: str
    severity: str = "WARNING"
    symbol: str = ""
    reference: str = ""

    def as_dict(self) -> dict[str, str]:
        return {name: str(getattr(self, name)) for name in self.__dataclass_fields__}


@dataclass
class ReconcileResult:
    scope: str
    differences: list[Difference] = field(default_factory=list)
    repaired: bool = False
    allows_resume: bool = False
    watermark: str | None = None
    cursor: str | None = None
    run_id: int | None = None

    @property
    def has_critical(self) -> bool:
        return any(d.severity == "CRITICAL" for d in self.differences)

    def summary(self) -> str:
        return (
            "；".join(f"[{d.severity}] {d.kind}: {d.detail}" for d in self.differences) or "无差异"
        )


class Reconciler:
    def __init__(
        self,
        *,
        adapter: TradingAdapter,
        sessionmaker,
        environment: str,
        account_uid: str,
        executor=None,
        execution_overlap_seconds: int = 300,
        max_differences: int = 0,
    ) -> None:
        self.adapter = adapter
        self.sessionmaker = sessionmaker
        self.environment = environment
        self.account_uid = account_uid
        self.executor = executor
        self.execution_overlap_seconds = execution_overlap_seconds
        self.max_differences = max_differences
        self.reservations = ReservationManager(environment)
        self.ledger = LedgerWriter(environment)

    async def _record(self, result: ReconcileResult, started: dt.datetime) -> ReconcileResult:
        result.allows_resume = not result.has_critical
        async with self.sessionmaker() as session:
            row = ReconciliationRun(
                environment=self.environment,
                scope=result.scope,
                started_at=started,
                finished_at=now_utc(),
                watermark=result.watermark,
                cursor=result.cursor,
                differences=[item.as_dict() for item in result.differences],
                repaired=result.repaired,
                allows_resume=result.allows_resume,
            )
            session.add(row)
            await session.commit()
            result.run_id = row.id
        return result

    async def reconcile(self) -> ReconcileResult:
        started = now_utc()
        results = [
            await self.reconcile_executions(),
            await self.reconcile_orders(),
            await self.reconcile_settlements(),
            await self.reconcile_positions(),
            await self.reconcile_cash(),
            await self.reconcile_reservations(),
        ]
        result = ReconcileResult(
            "full",
            differences=[item for part in results for item in part.differences],
            repaired=any(part.repaired for part in results),
            watermark=results[0].watermark,
            cursor=results[0].cursor,
        )
        return await self._record(result, started)

    async def reconcile_executions(self) -> ReconcileResult:
        started = now_utc()
        result = ReconcileResult("executions")
        async with self.sessionmaker() as session:
            previous = await session.scalar(
                select(ReconciliationRun)
                .where(
                    ReconciliationRun.environment == self.environment,
                    ReconciliationRun.scope == "executions",
                    ReconciliationRun.allows_resume.is_(True),
                )
                .order_by(ReconciliationRun.id.desc())
                .limit(1)
            )
            earliest = await session.scalar(
                select(func.min(TradeIntent.created_at)).where(
                    TradeIntent.environment == self.environment,
                )
            )
        # 所有返回事实都处理；重叠窗口只约束查询，不能丢弃离线期间旧成交。
        since = (dt.datetime.fromisoformat(previous.watermark)
                 if previous is not None and previous.watermark else
                 previous.started_at if previous is not None else earliest)
        if since is not None:
            since -= dt.timedelta(seconds=self.execution_overlap_seconds)
        for category in ("spot", "option"):
            try:
                import inspect

                parameters = inspect.signature(self.adapter.get_executions).parameters
                kwargs = (
                    {"start_time": since, "end_time": started}
                    if (since is not None and "start_time" in parameters)
                    else {}
                )
                executions = await self.adapter.get_executions(category, **kwargs)
                if (
                    since
                    and started - since > dt.timedelta(days=7)
                    and "start_time" not in parameters
                    and self.environment != "paper"
                ):
                    result.differences.append(
                        Difference(
                            "EXECUTION_HISTORY_GAP",
                            "离线超出默认历史窗口，尚未证明成交覆盖完整",
                            "CRITICAL",
                            reference=since.isoformat(),
                        )
                    )
            except Exception as exc:
                result.differences.append(
                    Difference(
                        "EXECUTION_QUERY_FAILED",
                        f"{category} 成交查询失败：{type(exc).__name__}",
                        "CRITICAL",
                    )
                )
                continue
            for execution in sorted(executions, key=lambda view: (view.exec_time, view.exec_id)):
                async with self.sessionmaker() as session:
                    known = await session.scalar(
                        select(Execution.id).where(
                            Execution.environment == self.environment,
                            Execution.category == category,
                            Execution.exchange_exec_id == execution.exec_id,
                        )
                    )
                if known is not None:
                    continue
                if self.executor is None:
                    result.differences.append(
                        Difference(
                            "MISSING_EXECUTION",
                            "缺失成交尚未入账",
                            "CRITICAL",
                            execution.symbol,
                            execution.exec_id,
                        )
                    )
                    continue
                if await self.executor.apply_execution(execution):
                    result.repaired = True
                async with self.sessionmaker() as session:
                    row = await session.scalar(
                        select(Execution).where(
                            Execution.environment == self.environment,
                            Execution.category == category,
                            Execution.exchange_exec_id == execution.exec_id,
                        )
                    )
                    if row is not None and row.order_id is None:
                        result.differences.append(
                            Difference(
                                "EXTERNAL_EXECUTION",
                                "成交不属于 bot，不自动归属或接管",
                                "CRITICAL",
                                execution.symbol,
                                execution.exec_id,
                            )
                        )
        result.watermark = started.isoformat()
        return await self._record(result, started)

    async def reconcile_orders(self) -> ReconcileResult:
        started = now_utc()
        result = ReconcileResult("orders")
        for category in ("spot", "option"):
            try:
                views = await self.adapter.get_open_orders(category)
            except Exception as exc:
                result.differences.append(
                    Difference(
                        "ORDER_QUERY_FAILED",
                        f"{category} 挂单查询失败：{type(exc).__name__}",
                        "CRITICAL",
                    )
                )
                continue
            async with self.sessionmaker() as session:
                orders = list(
                    await session.scalars(
                        select(Order).where(
                            Order.environment == self.environment,
                            Order.category == category,
                        )
                    )
                )
            by_link = {order.order_link_id: order for order in orders}
            exchange_links = {view.order_link_id for view in views}
            for view in views:
                if view.order_link_id not in by_link:
                    result.differences.append(
                        Difference(
                            "EXTERNAL_ORDER",
                            "发现非 bot 订单，不接管、不撤销",
                            "CRITICAL",
                            view.symbol,
                            view.order_link_id,
                        )
                    )
                elif self.executor is not None:
                    resolved = await self.executor.apply_order_view(view)
                    if not resolved.resolved:
                        result.differences.append(
                            Difference(
                                "ORDER_UNRESOLVED",
                                resolved.detail or "挂单事实仍待核对",
                                "CRITICAL",
                                view.symbol,
                                view.order_link_id,
                            )
                        )
            for order in orders:
                # 终态但累计成交未入账也必须重查，不能因终态跳过。
                if order.status in TERMINAL_STATES and order.sync_state == SYNC_CONFIRMED:
                    continue
                if order.order_link_id in exchange_links:
                    continue
                await self._resolve_missing_order(order, result)
        return await self._record(result, started)

    async def _resolve_missing_order(self, order: Order, result: ReconcileResult) -> None:
        if self.executor is not None:
            resolved = await self.executor.resolve_unknown(order)
            if resolved.resolved:
                result.repaired = True
                return
        elif order.status == PREPARED and order.sync_state == SYNC_NONE:
            async with self.sessionmaker() as session:
                row = await session.scalar(
                    select(Order).where(Order.id == order.id).with_for_update()
                )
                if (
                    row.status == PREPARED
                    and row.sync_state == SYNC_NONE
                    and not row.exchange_order_id
                    and row.qty_filled == 0
                ):
                    row.status, row.sync_state = CANCELED, SYNC_CONFIRMED
                    row.last_error = "持久化本地证据：发送器从未领取"
                    if row.reservation_id is not None:
                        await self.reservations.release(session, row.reservation_id)
                    await session.commit()
                    result.repaired = True
                    return
        # 没有可关联事实，明确保存未知同步状态，保留已有累计成交。
        async with self.sessionmaker() as session:
            row = await session.scalar(select(Order).where(Order.id == order.id).with_for_update())
            if row.status not in TERMINAL_STATES and row.qty_filled == 0:
                row.status = UNKNOWN
            row.sync_state = SYNC_UNCERTAIN
            await session.commit()
        result.differences.append(
            Difference(
                "ORDER_NOT_FOUND",
                "查询未得到充分事实；未知结果和预留继续保留",
                "CRITICAL",
                order.symbol,
                order.order_link_id,
            )
        )

    async def reconcile_settlements(self) -> ReconcileResult:
        """交割与普通卖出独立；现金及持仓均核对后才释放过期库存成本。"""
        started = now_utc()
        result = ReconcileResult("settlements")
        if self.environment == "paper":
            return await self._record(result, started)
        async with self.sessionmaker() as session:
            specs = list(
                await session.scalars(
                    select(Instrument).where(
                        Instrument.environment == self.environment,
                        Instrument.category == "option",
                        Instrument.delivery_time <= started,
                        Instrument.symbol.in_(
                            select(Inventory.symbol).where(
                                Inventory.environment == self.environment, Inventory.qty > 0
                            )
                        ),
                    )
                )
            )
        if not specs:
            return await self._record(result, started)
        try:
            if not hasattr(self.adapter, "get_option_deliveries"):
                raise ValueError("适配器缺少交割记录能力")
            account = await self.adapter.get_account()
            positions = await self.adapter.get_positions("option")
            if account.account_uid != self.account_uid or account.has_debt:
                raise ValueError("交割账户身份或自有现金不可信")
            candidates = []
            for spec in specs:
                view = InstrumentSpec(
                    category="option", symbol=spec.symbol,
                    base_coin=spec.base_coin, quote_coin=spec.quote_coin,
                    status=spec.status, settle_coin=spec.settle_coin,
                    strike_price=spec.strike_price, options_type=spec.options_type,
                    delivery_time=spec.delivery_time,
                )
                facts = await self.adapter.get_option_deliveries(
                    view,
                    start_time=spec.delivery_time - dt.timedelta(minutes=1),
                    end_time=min(started, spec.delivery_time + dt.timedelta(minutes=5)),
                )
                if len(facts) != 1:
                    raise ValueError("交割记录缺失或同合约记录不唯一")
                fact = facts[0]
                if (
                    fact.symbol != spec.symbol or fact.currency != spec.settle_coin
                    or fact.side != "Buy" or fact.qty <= 0
                    or fact.occurred_at < spec.delivery_time
                    or fact.occurred_at > started
                    or any(p.symbol == spec.symbol and p.size != 0 for p in positions)
                ):
                    raise ValueError("交割与完整账户持仓、时间或结算币不符")
                async with self.sessionmaker() as session:
                    rows = list(
                        await session.scalars(
                            select(Inventory).where(
                                Inventory.environment == self.environment,
                                Inventory.symbol == spec.symbol, Inventory.qty > 0,
                            ).order_by(Inventory.strategy_id)
                        )
                    )
                    active = await session.scalar(
                        select(Order.id).where(
                            Order.environment == self.environment, Order.symbol == spec.symbol,
                            (Order.status.notin_(list(TERMINAL_STATES)))
                            | (Order.sync_state != SYNC_CONFIRMED),
                        ).limit(1)
                    )
                if (
                    active is not None or not rows
                    or any(
                        row.cost_source != "bot_fill" or row.currency != fact.currency
                        for row in rows
                    )
                    or sum((row.qty for row in rows), ZERO) != fact.qty
                ):
                    raise ValueError("交割数量与可信归属不符或仍有活动/未知订单")
                candidates.append((fact, rows))
            cash = [
                (fact.currency, fact.payout - fact.fee, fact.occurred_at)
                for fact, _ in candidates
            ]
            check, anchor = await self._cash_evidence(account, pending_flows=cash)
            if anchor is None or check:
                raise ValueError("交割现金尚未与先前可信账户快照核对")
            # 每个候选都已核对后在同一事务入账，失败时不留下半次分配。
            async with self.sessionmaker() as session:
                for fact, rows in candidates:
                    allocations = []
                    payout_left, fee_left = fact.payout, fact.fee
                    for index, row in enumerate(rows):
                        payout = (
                            payout_left if index == len(rows) - 1
                            else fact.payout * row.qty / fact.qty
                        )
                        fee = (
                            fee_left if index == len(rows) - 1
                            else fact.fee * row.qty / fact.qty
                        )
                        payout_left -= payout
                        fee_left -= fee
                        allocations.append({
                            "strategy_id": row.strategy_id, "qty": str(row.qty),
                            "payout": str(payout), "fee": str(fee),
                        })
                    event = await self.ledger.record_option_settlement(
                        session,
                        settlement_id=fact.settlement_id, symbol=fact.symbol,
                        currency=fact.currency, occurred_at=fact.occurred_at,
                        allocations=allocations,
                        evidence={
                            **fact.evidence,
                            "source": "bybit", "reference": fact.reference,
                            "positions_reconciled": True, "balance_reconciled": True,
                            "cash_anchor_snapshot_id": anchor,
                            "delivery_price": str(fact.delivery_price),
                            "strike_price": str(fact.strike_price),
                        },
                    )
                    result.repaired = result.repaired or event is not None
                await session.commit()
        except Exception as exc:
            result.differences.append(Difference(
                "SETTLEMENT_UNVERIFIED",
                f"交割事实、现金或持仓未核实：{type(exc).__name__}", "CRITICAL",
            ))
        return await self._record(result, started)

    async def reconcile_positions(self) -> ReconcileResult:
        started = now_utc()
        result = ReconcileResult("positions")
        for category in ("spot", "option"):
            try:
                views = await self.adapter.get_positions(category)
                async with self.sessionmaker() as session:
                    existing = list(
                        await session.scalars(
                            select(Position).where(
                                Position.environment == self.environment,
                                Position.category == category,
                            )
                        )
                    )
                    inventory = list(
                        await session.scalars(
                            select(Inventory).where(
                                Inventory.environment == self.environment,
                            )
                        )
                    )
                    specs = list(
                        await session.scalars(
                            select(Instrument).where(
                                Instrument.environment == self.environment,
                                Instrument.category == category,
                            )
                        )
                    )
                if category == "spot":
                    account = await self.adapter.get_account()
                    symbols = {row.symbol for row in existing} | {row.symbol for row in inventory}
                    spec_by_symbol = {spec.symbol: spec for spec in specs}
                    views = [
                        PositionView(
                            category="spot",
                            symbol=symbol,
                            size=account.coin(spec_by_symbol[symbol].base_coin).wallet_balance
                            or ZERO,
                            side="Buy",
                        )
                        for symbol in symbols
                        if symbol in spec_by_symbol
                    ]
            except Exception as exc:
                result.differences.append(
                    Difference(
                        "POSITION_QUERY_FAILED",
                        f"{category} 持仓查询失败：{type(exc).__name__}",
                        "CRITICAL",
                    )
                )
                continue
            by_key = {(view.symbol, view.position_idx): view for view in views}
            # 完整响应中消失的旧持仓是数量为零的事实，不等于结算/成本释放证据。
            for row in existing:
                by_key.setdefault(
                    (row.symbol, row.position_idx),
                    PositionView(
                        category=category,
                        symbol=row.symbol,
                        position_idx=row.position_idx,
                        size=ZERO,
                    ),
                )
            for view in by_key.values():
                owned = [item for item in inventory if item.symbol == view.symbol and item.qty > 0]
                expected = sum((item.qty for item in owned), ZERO)
                matches = abs(expected - view.size) <= TOLERANCE
                trusted = (
                    matches
                    and all(item.trusted for item in owned)
                    and (view.size == 0 or (view.side or "").lower() == "buy")
                )
                if not trusted:
                    result.differences.append(
                        Difference(
                            "POSITION_DRIFT",
                            f"{view.symbol} 账户 {view.size}，bot 归属 {expected}；保留成本待核对",
                            "CRITICAL",
                            view.symbol,
                        )
                    )
                async with self.sessionmaker() as session:
                    await self.ledger.upsert_position(
                        session,
                        category=category,
                        symbol=view.symbol,
                        position_idx=view.position_idx,
                        side=view.side,
                        size=view.size,
                        avg_price=view.avg_price,
                        mark_price=view.mark_price,
                        settle_coin=view.settle_coin,
                        delivery_time=view.delivery_time,
                        delta=view.delta,
                        gamma=view.gamma,
                        vega=view.vega,
                        theta=view.theta,
                        unrealised_pnl=view.unrealised_pnl,
                        trusted=trusted,
                    )
                    if not trusted:
                        for item in await session.scalars(
                            select(Inventory)
                            .where(
                                Inventory.environment == self.environment,
                                Inventory.symbol == view.symbol,
                            )
                            .with_for_update()
                        ):
                            item.trusted = False
                    await session.commit()
        return await self._record(result, started)

    async def _cash_evidence(self, account, *, pending_flows=()):
        """从最近一次可信快照重放现金；未通过的快照不能吸收未解释变化。"""
        differences = []
        async with self.sessionmaker() as session:
            query = select(AccountSnapshot).where(
                AccountSnapshot.environment == self.environment,
                AccountSnapshot.account_uid == self.account_uid,
                AccountSnapshot.complete.is_(True),
            ).where(
                # 交割尚未记账期间的快照即使曾完整，也不能吸收该笔现金变化。
                ~select(Inventory.id).join(
                    Instrument,
                    (Instrument.environment == Inventory.environment)
                    & (Instrument.symbol == Inventory.symbol)
                    & (Instrument.category == "option"),
                ).where(
                    Inventory.environment == self.environment,
                    Inventory.qty > 0,
                    Instrument.delivery_time <= AccountSnapshot.captured_at,
                ).exists()
            )
            if pending_flows:
                query = query.where(
                    AccountSnapshot.captured_at < min(stamp for _, _, stamp in pending_flows)
                )
            previous = await session.scalar(
                query.order_by(AccountSnapshot.captured_at.desc()).limit(1)
            )
            if previous is None:
                return differences, None
            executions = list(await session.scalars(select(Execution).where(
                Execution.environment == self.environment,
                Execution.exec_time > previous.captured_at,
            )))
            flows = list(await session.scalars(select(CashFlow).where(
                CashFlow.environment == self.environment,
                CashFlow.occurred_at > previous.captured_at,
            )))
            specs = list(await session.scalars(select(Instrument).where(
                Instrument.environment == self.environment,
            )))
        expected = {
            coin: Decimal(str(value.get("wallet_balance", "NaN")))
            for coin, value in previous.coins.items()
            if isinstance(value, dict) and value.get("wallet_balance") is not None
        }
        by_symbol = {(spec.category, spec.symbol): spec for spec in specs}
        for execution in executions:
            spec = by_symbol.get((execution.category, execution.symbol))
            currency = None if spec is None else (
                spec.settle_coin if execution.category == "option" else spec.quote_coin
            )
            if currency is None:
                differences.append(Difference(
                    "CASH_UNQUANTIFIED", "成交缺少现金单位元数据", "CRITICAL", execution.symbol
                ))
                continue
            buying = execution.side.lower() == "buy"
            notional = execution.exec_qty * execution.exec_price
            expected[currency] = expected.get(currency, ZERO) + (-notional if buying else notional)
            if execution.category == "spot":
                expected[spec.base_coin] = expected.get(spec.base_coin, ZERO) + (
                    execution.exec_qty if buying else -execution.exec_qty
                )
            if execution.exec_fee:
                if not execution.exec_fee_asset:
                    differences.append(Difference(
                        "CASH_UNQUANTIFIED", "成交费用币种缺失", "CRITICAL", execution.symbol
                    ))
                else:
                    asset = execution.exec_fee_asset
                    expected[asset] = expected.get(asset, ZERO) - execution.exec_fee
        for flow in flows:
            expected[flow.asset] = expected.get(flow.asset, ZERO) + flow.amount
        for currency, amount, _ in pending_flows:
            expected[currency] = expected.get(currency, ZERO) + amount
        for coin in set(expected) | set(account.coins):
            actual = account.coin(coin).wallet_balance
            amount = expected.get(coin, ZERO)
            if (
                actual is None or not actual.is_finite() or not amount.is_finite()
                or abs(amount - actual) > TOLERANCE
            ):
                differences.append(Difference(
                    "EXTERNAL_CASH_CHANGE", f"{coin} 现金变化不能由已入账成交/资金流解释",
                    "CRITICAL", reference=coin,
                ))
        return differences, previous.id

    async def reconcile_cash(self) -> ReconcileResult:
        started = now_utc()
        result = ReconcileResult("cash")
        try:
            account = await self.adapter.get_account()
            if account.account_uid != self.account_uid or account.has_debt:
                result.differences.append(Difference(
                    "ACCOUNT_UNTRUSTED", "账户身份不匹配或存在负债", "CRITICAL",
                ))
            else:
                result.differences, _ = await self._cash_evidence(account)
        except Exception as exc:
            result.differences.append(Difference(
                "ACCOUNT_QUERY_FAILED", f"现金查询失败：{type(exc).__name__}", "CRITICAL",
            ))
        return await self._record(result, started)

    async def reconcile_reservations(self) -> ReconcileResult:
        started = now_utc()
        result = ReconcileResult("reservations")
        async with self.sessionmaker() as session:
            orders = list(
                await session.scalars(
                    select(Order).where(
                        Order.environment == self.environment,
                    )
                )
            )
            reservations = list(
                await session.scalars(
                    select(Reservation).where(
                        Reservation.environment == self.environment,
                        Reservation.status.in_(
                            [ReservationStatus.HELD, ReservationStatus.PARTIALLY_CONSUMED]
                        ),
                    )
                )
            )
            by_reservation: dict[int, list[Order]] = {}
            for order in orders:
                if order.reservation_id is not None:
                    by_reservation.setdefault(order.reservation_id, []).append(order)
            for reservation in reservations:
                linked = by_reservation.get(reservation.id, [])
                if not linked:
                    # 与提交器使用同一意图行锁，避免释放即将创建的 PREPARED 预留。
                    intent = await session.scalar(
                        select(TradeIntent)
                        .where(
                            TradeIntent.id == reservation.intent_id,
                        )
                        .with_for_update()
                    )
                    linked = list(
                        await session.scalars(
                            select(Order).where(
                                Order.reservation_id == reservation.id,
                            )
                        )
                    )
                    if not linked and intent is not None:
                        await self.reservations.release(session, reservation.id)
                        intent.status = "CANCELED_UNSENT"
                        await self.ledger.append_event(
                            session,
                            event_type="UNSENT_RESERVATION_RELEASE",
                            dedupe_key=f"unsent-reservation:{reservation.id}",
                            payload={
                                "category": intent.category,
                                "intent_id": intent.id,
                                "reservation_id": reservation.id,
                                "evidence": "意图行锁下不存在物理订单，发送器尚未领取",
                            },
                        )
                        result.repaired = True
                        continue
                complete = bool(linked)
                for order in linked:
                    accounted = await session.scalar(
                        select(func.coalesce(func.sum(Execution.exec_qty), 0)).where(
                            Execution.order_id == order.id
                        )
                    )
                    if (
                        order.status not in TERMINAL_STATES
                        or order.sync_state != SYNC_CONFIRMED
                        or accounted < order.qty_filled
                    ):
                        complete = False
                        if order.status in TERMINAL_STATES:
                            result.differences.append(
                                Difference(
                                    "TERMINAL_EXECUTIONS_PENDING",
                                    "终态累计成交未完整入账，继续预留",
                                    "CRITICAL",
                                    order.symbol,
                                    order.order_link_id,
                                )
                            )
                if complete:
                    await self.reservations.release(session, reservation.id)
                    result.repaired = True
            for order in orders:
                if order.status in TERMINAL_STATES and order.sync_state == SYNC_CONFIRMED:
                    continue
                reservation = (
                    await session.get(Reservation, order.reservation_id)
                    if (order.reservation_id is not None)
                    else None
                )
                if reservation is None or reservation.status == ReservationStatus.RELEASED:
                    result.differences.append(
                        Difference(
                            "MISSING_RESERVATION",
                            "活动/未知订单缺少有效预留",
                            "CRITICAL",
                            order.symbol,
                            order.order_link_id,
                        )
                    )
            await session.commit()
        return await self._record(result, started)


__all__ = ["Difference", "ReconcileResult", "Reconciler"]
