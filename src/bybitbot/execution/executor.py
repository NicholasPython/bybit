"""统一执行与逐笔记账。写请求先持久化领取记录，未知结果从不自动重发。"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bybitbot.adapters.base import (
    DUPLICATE_ORDER_LINK_RETCODES,
    AccountView,
    ExecutionView,
    OrderAck,
    OrderRejected,
    OrderRequest,
    OrderUpdate,
    OrderView,
    TradingAdapter,
)
from bybitbot.clock import now_utc
from bybitbot.db.models import Execution, Instrument, Order, Reservation, RiskPause, TradeIntent
from bybitbot.execution.lease import ExecutorLease, LeaseState
from bybitbot.execution.states import (
    CANCELED,
    FILLED,
    PARTIALLY_FILLED,
    PREPARED,
    REJECTED,
    SUBMITTING,
    SYNC_CONFIRMED,
    SYNC_NONE,
    SYNC_PENDING,
    SYNC_UNCERTAIN,
    TERMINAL_STATES,
    UNKNOWN,
    OrderState,
    apply_transition,
    effective_status,
)
from bybitbot.ledger import LedgerWriter, order_link_id_for
from bybitbot.money import dec
from bybitbot.risk.reservation import ReservationManager

logger = logging.getLogger(__name__)
ZERO = Decimal("0")


class SubmitOutcome(StrEnum):
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


class ExecutionAuthorityError(RuntimeError):
    pass


class UnknownOrderError(RuntimeError):
    pass


@dataclass(frozen=True)
class OrderPlan:
    intent_id: int
    category: str
    symbol: str
    side: str
    order_type: str = "Limit"
    time_in_force: str = "GTC"
    qty: Decimal = ZERO
    price: Decimal = ZERO
    reduce_only: bool = False
    position_idx: int | None = None
    strategy_id: int | None = None
    spend_limit: Decimal = ZERO
    reservation_id: int | None = None
    local_ttl_expires_at: dt.datetime | None = None


@dataclass
class SubmitResult:
    outcome: SubmitOutcome
    order: Order | None = None
    ack: OrderAck | None = None
    detail: str = ""

    @property
    def is_unknown(self) -> bool:
        return self.outcome is SubmitOutcome.UNKNOWN


@dataclass
class Resolution:
    resolved: bool
    state: OrderState
    detail: str = ""
    evidence: dict[str, object] = field(default_factory=dict)


class Executor:
    def __init__(
        self,
        *,
        adapter: TradingAdapter,
        sessionmaker,
        environment: str,
        account_uid: str,
        ledger: LedgerWriter | None = None,
        lease: ExecutorLease | None = None,
        cancel_verify_timeout: float = 10.0,
        cancel_poll_interval: float = 1.0,
    ) -> None:
        self.adapter = adapter
        self.sessionmaker = sessionmaker
        self.environment = environment
        self.account_uid = account_uid
        self.ledger = ledger or LedgerWriter(environment)
        self.reservations = ReservationManager(environment)
        self.lease = lease
        self.cancel_verify_timeout = cancel_verify_timeout
        self.cancel_poll_interval = cancel_poll_interval

    async def _require_authority(self) -> None:
        from bybitbot.adapters.paper import PaperAdapter

        is_paper = isinstance(self.adapter, PaperAdapter) and self.environment == "paper"
        if self.adapter.environment != self.environment:
            raise ExecutionAuthorityError("适配器环境与执行器环境不匹配")
        if not is_paper and not self.adapter.allow_trade_writes:
            raise ExecutionAuthorityError("适配器不允许交易写请求")
        if self.lease is None:
            if is_paper:
                return  # 仅进程内纸面测试；远程适配器不能绕过租约。
            raise ExecutionAuthorityError("远程交易必须持有执行者租约")
        if self.lease.environment != self.environment or self.lease.account_uid != self.account_uid:
            raise ExecutionAuthorityError("租约账户或环境不匹配")
        status = await self.lease.heartbeat()
        if status.state is not LeaseState.ELIGIBLE:
            raise ExecutionAuthorityError(f"执行资格不足：{status.state} {status.detail}")

    async def submit(
        self, plan: OrderPlan, *, rebuild_index: int = 0, parent_order_id: int | None = None
    ) -> SubmitResult:
        await self._require_authority()
        if (
            not dec(plan.qty).is_finite()
            or not dec(plan.price).is_finite()
            or plan.qty <= 0
            or plan.price <= 0
            or plan.order_type.lower() != "limit"
        ):
            raise ExecutionAuthorityError("只允许数量、价格为正的有限限价单")
        # 同一意图锁覆盖去重与物理记录创建，跨数据库使用随机全局编号。
        async with self.sessionmaker() as session:
            intent = await session.scalar(
                select(TradeIntent)
                .where(
                    TradeIntent.id == plan.intent_id,
                    TradeIntent.environment == self.environment,
                    TradeIntent.account_uid == self.account_uid,
                )
                .with_for_update()
            )
            if intent is None:
                raise ExecutionAuthorityError("意图不属于当前账户/环境")
            existing = await session.scalar(
                select(Order).where(
                    Order.intent_id == intent.id,
                    Order.environment == self.environment,
                    Order.rebuild_index == rebuild_index,
                )
            )
            if existing is not None:
                return self._existing_result(existing)
            await self._validate_plan(session, intent, plan, rebuild_index, parent_order_id)
            link_id = order_link_id_for(plan.intent_id, rebuild_index)
            order = Order(
                environment=self.environment,
                intent_id=intent.id,
                order_link_id=link_id,
                category=plan.category,
                symbol=plan.symbol,
                side=plan.side.lower(),
                order_type=plan.order_type,
                time_in_force=plan.time_in_force,
                price=dec(plan.price),
                qty=dec(plan.qty),
                qty_filled=ZERO,
                status=PREPARED,
                sync_state=SYNC_NONE,
                reduce_only=plan.reduce_only,
                position_idx=plan.position_idx,
                parent_order_id=parent_order_id,
                rebuild_index=rebuild_index,
                reservation_id=plan.reservation_id,
                local_ttl_expires_at=plan.local_ttl_expires_at,
            )
            session.add(order)
            await session.flush()
            await self.reservations.bind_order(session, plan.reservation_id, order.id)
            await session.commit()
            order_id = order.id

        await self._require_authority()
        async with self.sessionmaker() as session:
            row = await self._get_order(session, order_id)
            if row.status != PREPARED or row.sync_state != SYNC_NONE:
                return self._existing_result(row)
            intent = await session.get(TradeIntent, row.intent_id)
            if now_utc() >= intent.expires_at:
                row.status = CANCELED
                row.last_error = "信号在发送器领取之前过期；本地证据证明从未发送"
                await self.reservations.release(session, row.reservation_id)
                await session.commit()
                return SubmitResult(SubmitOutcome.REJECTED, order=row, detail=row.last_error)
            row.status = SUBMITTING
            row.sync_state = SYNC_PENDING
            await session.commit()

        request = OrderRequest(
            category=plan.category,
            symbol=plan.symbol,
            side=_side_enum(plan.side),
            order_type=_type_enum(plan.order_type),
            qty=dec(plan.qty),
            price=dec(plan.price),
            time_in_force=_tif_enum(plan.time_in_force),
            order_link_id=link_id,
            reduce_only=plan.reduce_only,
            position_idx=plan.position_idx,
        )
        try:
            # 领取记录已提交；这里失权/断连同样按结果未知处理，绝不重发。
            await self._require_authority()
            ack = await self.adapter.create_order(request)
        except OrderRejected as exc:
            if exc.ret_code in DUPLICATE_ORDER_LINK_RETCODES:
                return await self._mark_unknown(order_id, f"重复编号不是未受理证据：{exc}")
            return await self._mark_rejected(order_id, exc)
        except asyncio.CancelledError:
            await asyncio.shield(self._mark_unknown(order_id, "发送任务被终止，结果待核实"))
            raise
        except Exception as exc:
            return await self._mark_unknown(order_id, f"写请求结果未知：{exc}")
        if ack.order_link_id != link_id:
            return await self._mark_unknown(order_id, "回执订单编号不匹配")
        async with self.sessionmaker() as session:
            row = await self._get_order(session, order_id)
            if row.exchange_order_id and row.exchange_order_id != ack.order_id:
                await self._pause(session, "UNKNOWN_ORDER", "回执交易所编号冲突")
                row.sync_state = SYNC_UNCERTAIN
            else:
                row.exchange_order_id = ack.order_id or row.exchange_order_id
                # 回执不是累计成交凭证，不能把先到的推送状态/成交覆盖成 New。
                if row.status == SUBMITTING:
                    target = effective_status(
                        exchange_status=ack.status, qty=row.qty, qty_filled=row.qty_filled
                    )
                    if target in TERMINAL_STATES:
                        row.sync_state = SYNC_UNCERTAIN
                    else:
                        row.status = target
                        row.sync_state = SYNC_CONFIRMED if target != UNKNOWN else SYNC_UNCERTAIN
            await session.commit()
        return SubmitResult(SubmitOutcome.ACCEPTED, order=row, ack=ack)

    async def _validate_plan(
        self,
        session: AsyncSession,
        intent: TradeIntent,
        plan: OrderPlan,
        rebuild_index: int,
        parent_order_id: int | None,
    ) -> None:
        if intent.status != "APPROVED":
            raise ExecutionAuthorityError("意图尚未批准或已由恢复流程终止")
        if (
            intent.symbol != plan.symbol
            or intent.category != plan.category
            or intent.side.lower() != plan.side.lower()
        ):
            raise ExecutionAuthorityError("计划与原意图不匹配")
        if now_utc() >= intent.expires_at:
            raise ExecutionAuthorityError("信号已过期")
        if plan.category == "option" and plan.side.lower() == "sell" and not plan.reduce_only:
            raise ExecutionAuthorityError("期权卖出必须 reduceOnly")
        if (
            intent.price_limit is None
            or (plan.side.lower() == "buy" and plan.price > intent.price_limit)
            or (plan.side.lower() == "sell" and plan.price < intent.price_limit)
        ):
            raise ExecutionAuthorityError("价格突破意图边界")
        if plan.reservation_id is None:
            raise ExecutionAuthorityError("订单必须关联持久化预留")
        reservation = await self.reservations._get(session, plan.reservation_id)
        required = plan.qty * plan.price if plan.side.lower() == "buy" else plan.qty
        kind = "CASH" if plan.side.lower() == "buy" else "INVENTORY"
        if (
            reservation.intent_id != intent.id
            or reservation.kind != kind
            or reservation.amount - reservation.consumed - reservation.released < required
        ):
            raise ExecutionAuthorityError("预留与意图/方向不匹配或额度不足")
        orders = list(await session.scalars(select(Order).where(Order.intent_id == intent.id)))
        for order in orders:
            if order.status not in TERMINAL_STATES or order.sync_state != SYNC_CONFIRMED:
                raise ExecutionAuthorityError("同意图仍有活动/未知订单，禁止创建第二张物理单")
            if await self._accounted_qty(session, order.id) < order.qty_filled:
                raise ExecutionAuthorityError("原订单累计成交尚未逐笔入账，禁止重建")
        if rebuild_index > 0:
            parent = next((o for o in orders if o.id == parent_order_id), None)
            if (
                parent is None
                or parent.status == FILLED
                or parent.rebuild_index + 1 != rebuild_index
            ):
                raise ExecutionAuthorityError("重建父订单无效")
        elif orders or parent_order_id is not None:
            raise ExecutionAuthorityError("初始物理订单已存在")
        filled = sum((o.qty_filled for o in orders), ZERO)
        if filled + plan.qty > intent.qty:
            raise ExecutionAuthorityError("累计数量超过原意图")
        if plan.side.lower() == "buy":
            spent = await self._intent_spend(session, intent.id, intent.currency)
            limit = (
                min(intent.spend_limit, plan.spend_limit)
                if plan.spend_limit
                else intent.spend_limit
            )
            if spent + plan.qty * plan.price > limit:
                raise ExecutionAuthorityError("累计支出超过原意图")

    @staticmethod
    def _existing_result(row: Order) -> SubmitResult:
        outcome = SubmitOutcome.ACCEPTED
        if row.sync_state in {SYNC_PENDING, SYNC_UNCERTAIN} or row.status in {UNKNOWN, SUBMITTING}:
            outcome = SubmitOutcome.UNKNOWN
        elif row.status in {REJECTED, PREPARED} or row.sync_state == SYNC_NONE:
            outcome = SubmitOutcome.REJECTED
        return SubmitResult(outcome, order=row, detail="已有持久化物理订单；没有再次发送")

    async def _mark_unknown(self, order_id: int, detail: str) -> SubmitResult:
        async with self.sessionmaker() as session:
            row = await self._get_order(session, order_id)
            if row.status not in TERMINAL_STATES and row.qty_filled == 0:
                row.status = UNKNOWN
            row.sync_state = SYNC_UNCERTAIN
            row.last_error = detail
            await self._pause(session, "UNKNOWN_ORDER", detail)
            await session.commit()
        return SubmitResult(SubmitOutcome.UNKNOWN, order=row, detail=detail)

    async def _mark_rejected(self, order_id: int, exc: OrderRejected) -> SubmitResult:
        async with self.sessionmaker() as session:
            row = await self._get_order(session, order_id)
            if row.exchange_order_id or row.qty_filled > 0 or row.status != SUBMITTING:
                row.sync_state = SYNC_UNCERTAIN
                await self._pause(session, "UNKNOWN_ORDER", "拒绝响应与既有受理/成交事实冲突")
                await session.commit()
                return SubmitResult(SubmitOutcome.UNKNOWN, order=row, detail=str(exc))
            row.status = REJECTED
            row.sync_state = SYNC_CONFIRMED
            row.last_error = str(exc)
            await self.reservations.release(session, row.reservation_id)
            await session.commit()
        return SubmitResult(SubmitOutcome.REJECTED, order=row, detail=str(exc))

    async def cancel(self, order: Order) -> Resolution:
        await self._require_authority()
        async with self.sessionmaker() as session:
            order = await self._get_order(session, order.id)
            if order.status in TERMINAL_STATES and order.sync_state == SYNC_CONFIRMED:
                return Resolution(True, self._state_of(order), "已确认终态")
        try:
            await self.adapter.cancel_order(
                order.category,
                order.symbol,
                order_id=order.exchange_order_id,
                order_link_id=order.order_link_id,
            )
        except OrderRejected:
            pass  # 已成交/不存在等错误都需要事实核实。
        except Exception as exc:
            result = await self._mark_unknown(order.id, f"撤单结果待核实：{exc}")
            return Resolution(False, self._state_of(result.order), result.detail)
        return await self.verify_terminal(order)

    async def verify_terminal(self, order: Order) -> Resolution:
        deadline = time.monotonic() + self.cancel_verify_timeout
        while True:
            try:
                views = await self.adapter.get_order_history(
                    order.category, order_link_ids=[order.order_link_id]
                )
                for view in views:
                    if (
                        effective_status(
                            exchange_status=view.status, qty=view.qty, qty_filled=view.qty_filled
                        )
                        in TERMINAL_STATES
                    ):
                        return await self.apply_order_view(view)
            except Exception as exc:
                logger.warning("撤单核实查询失败 %s: %s", order.order_link_id, exc)
            if time.monotonic() >= deadline:
                return Resolution(False, self._state_of(order), "未取得终态与完整成交，保留预留")
            await asyncio.sleep(min(self.cancel_poll_interval, max(0, deadline - time.monotonic())))

    async def apply_order_view(self, view: OrderView) -> Resolution:
        async with self.sessionmaker() as session:
            row = await self._find_order(session, view.order_link_id)
            if row is None:
                return Resolution(False, OrderState(UNKNOWN, SYNC_UNCERTAIN), "非本地订单")
            if (
                view.category != row.category
                or view.symbol != row.symbol
                or (
                    view.order_id
                    and row.exchange_order_id
                    and view.order_id != row.exchange_order_id
                )
                or view.qty != row.qty
                or not ZERO <= view.qty_filled <= row.qty
            ):
                await self._pause(
                    session, "RECONCILIATION_DIFFERENCE", "订单事实与本地身份/数量冲突"
                )
                row.sync_state = SYNC_UNCERTAIN
                await session.commit()
                return Resolution(False, self._state_of(row), "订单事实冲突")
            target = effective_status(
                exchange_status=view.status, qty=view.qty, qty_filled=view.qty_filled
            )
            if target == UNKNOWN:
                row.sync_state = SYNC_UNCERTAIN
                await self._pause(session, "UNKNOWN_ORDER", "未识别交易所状态")
                await session.commit()
                return Resolution(False, self._state_of(row), "未识别交易所状态")
            state = apply_transition(
                self._state_of(row),
                target=target,
                qty_filled=max(row.qty_filled, view.qty_filled),
                confirmed=True,
            )
            row.status, row.qty_filled = state.status, state.qty_filled
            row.sync_state = SYNC_CONFIRMED if target == FILLED else state.sync_state
            row.exchange_order_id = view.order_id or row.exchange_order_id
            if view.avg_price is not None:
                row.avg_price = view.avg_price
            accounted = await self._accounted_qty(session, row.id)
            if target == FILLED and view.qty_filled != row.qty:
                row.sync_state = SYNC_UNCERTAIN
                await self._pause(
                    session, "RECONCILIATION_DIFFERENCE", "全成状态缺少完整累计成交证据"
                )
            elif accounted < row.qty_filled:
                row.sync_state = SYNC_UNCERTAIN
                await self._pause(session, "RECONCILIATION_DIFFERENCE", "累计成交尚未完整逐笔入账")
            elif state.is_terminal and row.reservation_id is not None:
                await self.reservations.release(session, row.reservation_id)
            await session.commit()
            return Resolution(row.sync_state == SYNC_CONFIRMED, self._state_of(row))

    async def apply_order_update(self, update: OrderUpdate) -> Resolution:
        return await self.apply_order_view(
            OrderView(
                order_id=update.order_id,
                order_link_id=update.order_link_id,
                category=update.category,
                symbol=update.symbol,
                side="",
                order_type="",
                price=update.price,
                qty=update.qty,
                qty_filled=update.qty_filled,
                status=update.status,
                raw=update.raw,
            )
        )

    async def apply_execution(self, execution: ExecutionView) -> bool:
        if (
            not execution.exec_qty.is_finite()
            or not execution.exec_price.is_finite()
            or execution.exec_qty <= 0
            or execution.exec_price <= 0
            or (execution.exec_fee is not None and not execution.exec_fee.is_finite())
        ):
            raise ValueError("实际成交价格/数量/费用必须为有限有效值")
        async with self.sessionmaker() as session:
            row = (
                await self._find_order(session, execution.order_link_id)
                if execution.order_link_id
                else None
            )
            if row is None and execution.order_id:
                row = await session.scalar(
                    select(Order)
                    .where(
                        Order.environment == self.environment,
                        Order.category == execution.category,
                        Order.exchange_order_id == execution.order_id,
                    )
                    .with_for_update()
                )
            if row is not None and (
                row.category != execution.category
                or row.symbol != execution.symbol
                or row.side.lower() != execution.side.lower()
                or (
                    row.exchange_order_id
                    and execution.order_id
                    and row.exchange_order_id != execution.order_id
                )
            ):
                raise ValueError("成交身份与本地订单冲突")
            recorded = await self.ledger.record_execution(
                session,
                exchange_exec_id=execution.exec_id,
                category=execution.category,
                exchange_order_id=execution.order_id,
                order_link_id=execution.order_link_id,
                order_id=row.id if row else None,
                symbol=execution.symbol,
                side=execution.side,
                exec_price=execution.exec_price,
                exec_qty=execution.exec_qty,
                exec_fee=execution.exec_fee,
                exec_fee_asset=execution.exec_fee_asset,
                is_maker=execution.is_maker,
                exec_time=execution.exec_time,
                raw=execution.raw,
            )
            if recorded is None:
                return False
            if row is None:
                await self._pause(session, "EXTERNAL_ACCOUNT_CHANGE", "发现未归属 bot 的实际成交")
            else:
                accounted = await self._accounted_qty(session, row.id)
                row.qty_filled = max(row.qty_filled, accounted)
                if execution.order_id:
                    row.exchange_order_id = execution.order_id
                was_terminal = row.status in TERMINAL_STATES
                if not was_terminal or accounted == row.qty:
                    row.status = FILLED if accounted >= row.qty else PARTIALLY_FILLED
                if accounted > row.qty:
                    await self._pause(
                        session, "RECONCILIATION_DIFFERENCE", "实际成交超过物理订单数量"
                    )
                    row.sync_state = SYNC_UNCERTAIN
                elif accounted == row.qty or (was_terminal and accounted >= row.qty_filled):
                    row.sync_state = SYNC_CONFIRMED
                reservation = (
                    await self.reservations._get(session, row.reservation_id)
                    if row.reservation_id is not None
                    else None
                )
                if reservation is not None:
                    amount = await self._consume_amount(session, row, execution, reservation)
                    if reservation.consumed + amount > reservation.amount:
                        await self._pause(session, "RECONCILIATION_DIFFERENCE", "实际消耗超过预留")
                    await self.reservations.consume(
                        session, reservation.id, amount, allow_overrun=True
                    )
                else:
                    await self._pause(session, "RECONCILIATION_DIFFERENCE", "成交缺少对应预留")
                await self._apply_inventory(session, row, execution)
                if (
                    row.status in TERMINAL_STATES
                    and accounted >= row.qty_filled
                    and reservation is not None
                    and row.sync_state == SYNC_CONFIRMED
                ):
                    await self.reservations.release(session, reservation.id)
            await self.ledger.append_event(
                session,
                event_type="EXECUTION",
                dedupe_key=f"exec:{execution.category}:{execution.exec_id}",
                payload={
                    "category": execution.category,
                    "symbol": execution.symbol,
                    "side": execution.side,
                    "exec_price": str(execution.exec_price),
                    "exec_qty": str(execution.exec_qty),
                    "exec_fee": str(execution.exec_fee),
                    "exec_fee_asset": execution.exec_fee_asset,
                    "exec_id": execution.exec_id,
                },
                occurred_at=execution.exec_time,
            )
            await session.commit()
        return True

    async def _apply_inventory(
        self, session: AsyncSession, row: Order, execution: ExecutionView
    ) -> None:
        intent = await session.get(TradeIntent, row.intent_id)
        spec = await session.scalar(
            select(Instrument).where(
                Instrument.environment == self.environment,
                Instrument.category == row.category,
                Instrument.symbol == row.symbol,
            )
        )
        _, realised = await self.ledger.apply_fill_to_inventory(
            session,
            strategy_id=intent.strategy_id,
            symbol=row.symbol,
            side=execution.side,
            qty=execution.exec_qty,
            price=execution.exec_price,
            fee=execution.exec_fee or ZERO,
            fee_asset=execution.exec_fee_asset,
            currency=intent.currency,
            category=row.category,
            base_asset=spec.base_coin if spec else None,
        )
        fee_asset = (execution.exec_fee_asset or intent.currency).upper()
        base_asset = (
            spec.base_coin
            if spec
            else (
                row.symbol[: -len(intent.currency)]
                if row.category == "spot" and row.symbol.endswith(intent.currency)
                else None
            )
        )
        if execution.exec_fee and fee_asset not in {intent.currency, base_asset}:
            await self._pause(
                session, "RECONCILIATION_DIFFERENCE", "跨币费用缺少估值；原币费用已保存"
            )
        if realised:
            await self.ledger.append_event(
                session,
                event_type="REALISED_PNL",
                dedupe_key=f"pnl:{row.category}:{execution.exec_id}",
                payload={
                    "category": row.category,
                    "symbol": row.symbol,
                    "strategy_id": intent.strategy_id,
                    "realised": str(realised),
                    "currency": intent.currency,
                },
                occurred_at=execution.exec_time,
            )

    async def _consume_amount(
        self, session: AsyncSession, row: Order, execution: ExecutionView, reservation: Reservation
    ) -> Decimal:
        fee = execution.exec_fee or ZERO
        asset = (execution.exec_fee_asset or "").upper()
        if reservation.kind == "CASH":
            return execution.exec_qty * execution.exec_price + (
                fee if asset == reservation.currency else ZERO
            )
        intent = await session.get(TradeIntent, row.intent_id)
        spec = await session.scalar(
            select(Instrument).where(
                Instrument.environment == self.environment,
                Instrument.category == row.category,
                Instrument.symbol == row.symbol,
            )
        )
        base = spec.base_coin if spec else row.symbol.removesuffix(intent.currency)
        return execution.exec_qty + (fee if row.category == "spot" and asset == base else ZERO)

    async def rebuild_bounds(self, intent_id: int) -> tuple[Decimal, Decimal, Decimal]:
        async with self.sessionmaker() as session:
            intent = await session.get(TradeIntent, intent_id)
            if intent is None or intent.environment != self.environment:
                raise ExecutionAuthorityError("意图不存在")
            orders = list(
                await session.scalars(
                    select(Order).where(
                        Order.environment == self.environment, Order.intent_id == intent_id
                    )
                )
            )
            filled = sum((o.qty_filled for o in orders), ZERO)
            open_qty = sum(
                (
                    max(ZERO, o.qty - o.qty_filled)
                    for o in orders
                    if o.status not in TERMINAL_STATES or o.sync_state != SYNC_CONFIRMED
                ),
                ZERO,
            )
            spend = await self._intent_spend(session, intent_id, intent.currency)
            return filled, open_qty, spend

    async def rebuild(self, *, parent: Order, plan: OrderPlan, max_rebuilds: int) -> SubmitResult:
        if parent.rebuild_index >= max_rebuilds or plan.intent_id != parent.intent_id:
            raise ExecutionAuthorityError("重建次数或意图无效")
        return await self.submit(
            plan, rebuild_index=parent.rebuild_index + 1, parent_order_id=parent.id
        )

    async def resolve_unknown(self, order: Order) -> Resolution:
        async with self.sessionmaker() as session:
            row = await self._get_order(session, order.id)
            # 使用锁定后的持久化事实；调用者持有的旧 PREPARED 对象不能证明没发过。
            if (
                row.status == PREPARED
                and row.sync_state == SYNC_NONE
                and row.exchange_order_id is None
                and row.qty_filled == 0
            ):
                row.status = CANCELED
                row.sync_state = SYNC_CONFIRMED
                row.last_error = "持久化本地证据：发送器从未领取"
                if row.reservation_id is not None:
                    await self.reservations.release(session, row.reservation_id)
                await session.commit()
                return Resolution(True, self._state_of(row), row.last_error)
            order = row
        views = await self.adapter.get_order_history(
            order.category, order_link_ids=[order.order_link_id]
        )
        for view in views:
            if view.order_link_id == order.order_link_id:
                result = await self.apply_order_view(view)
                if result.resolved:
                    return result
        executions = await self.adapter.get_executions(
            order.category, order_link_ids=[order.order_link_id]
        )
        for execution in executions:
            await self.apply_execution(execution)
        async with self.sessionmaker() as session:
            row = await self._get_order(session, order.id)
            resolved = row.status in TERMINAL_STATES and row.sync_state == SYNC_CONFIRMED
            return Resolution(
                resolved,
                self._state_of(row),
                "实际成交已入账；未确认剩余订单事实前保留未知与预留"
                if executions
                else "查询未找到不是未受理证明；保留 UNKNOWN 与预留",
            )

    async def fetch_account(self) -> AccountView:
        return await self.adapter.get_account()

    async def _find_order(self, session: AsyncSession, order_link_id: str) -> Order | None:
        return await session.scalar(
            select(Order)
            .where(Order.environment == self.environment, Order.order_link_id == order_link_id)
            .with_for_update()
        )

    async def _get_order(self, session: AsyncSession, order_id: int) -> Order:
        row = await session.scalar(
            select(Order)
            .where(Order.id == order_id, Order.environment == self.environment)
            .with_for_update()
        )
        if row is None:
            raise ValueError("订单不存在或环境不匹配")
        return row

    @staticmethod
    async def _accounted_qty(session: AsyncSession, order_id: int) -> Decimal:
        return dec(
            await session.scalar(
                select(func.coalesce(func.sum(Execution.exec_qty), 0)).where(
                    Execution.order_id == order_id
                )
            )
        )

    @staticmethod
    async def _intent_spend(session: AsyncSession, intent_id: int, currency: str) -> Decimal:
        fills = list(
            await session.scalars(
                select(Execution)
                .join(Order, Execution.order_id == Order.id)
                .where(Order.intent_id == intent_id)
            )
        )
        return sum(
            (
                e.exec_price * e.exec_qty
                + ((e.exec_fee or ZERO) if e.exec_fee_asset == currency else ZERO)
                for e in fills
            ),
            ZERO,
        )

    async def _pause(self, session: AsyncSession, code: str, detail: str) -> None:
        existing = await session.scalar(
            select(RiskPause.id)
            .where(
                RiskPause.environment == self.environment,
                RiskPause.scope == "ACCOUNT",
                RiskPause.reason_code == code,
                RiskPause.active.is_(True),
            )
            .limit(1)
        )
        if existing is None:
            session.add(
                RiskPause(
                    environment=self.environment,
                    scope="ACCOUNT",
                    scope_key="",
                    reason_code=code,
                    reason_detail=detail,
                    locked=True,
                    active=True,
                )
            )

    @staticmethod
    def _state_of(row: Order) -> OrderState:
        return OrderState(status=row.status, sync_state=row.sync_state, qty_filled=row.qty_filled)


def _side_enum(value: str):
    from bybitbot.settings.models import OrderSide

    return next(v for v in OrderSide if v.value.lower() == value.lower())


def _type_enum(value: str):
    from bybitbot.settings.models import OrderType

    return next(v for v in OrderType if v.value.lower() == value.lower())


def _tif_enum(value: str):
    from bybitbot.settings.models import TimeInForce

    return next(v for v in TimeInForce if v.value.lower() == value.lower())


__all__ = [
    "ExecutionAuthorityError",
    "Executor",
    "OrderPlan",
    "Resolution",
    "SubmitOutcome",
    "SubmitResult",
    "UnknownOrderError",
]
