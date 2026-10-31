"""原子现金/库存预留（RSK-02、SPT-02、OPT-02）。

交易所冻结与本地预留只在快照明确关联同一订单时抵扣；受理回执本身不证明
冻结已包含在余额快照中。所有未知、待发送及部分成交余量继续占用额度。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from bybitbot.adapters.base import AccountView
from bybitbot.db.models import Inventory, Order, Reservation, TradeIntent
from bybitbot.money import dec
from bybitbot.settings.models import ReservationStatus

ZERO = Decimal("0")
_ACTIVE = (ReservationStatus.HELD, ReservationStatus.PARTIALLY_CONSUMED)


@dataclass(frozen=True)
class BudgetView:
    currency: str
    own_cash: Decimal
    exchange_available: Decimal
    local_held: Decimal
    pending_held: Decimal
    usable: Decimal

    def as_dict(self) -> dict[str, str]:
        return {name: str(getattr(self, name)) for name in self.__dataclass_fields__}


class ReservationError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


async def _lock_cash(session: AsyncSession, environment: str, currency: str) -> None:
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
        {"key": f"{environment}:cash:{currency.upper()}"},
    )


async def _lock_inventory(session: AsyncSession, environment: str, symbol: str) -> None:
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
        {"key": f"{environment}:inv:{symbol}"},
    )


def _remaining(reservation: Reservation) -> Decimal:
    return max(ZERO, reservation.amount - reservation.consumed - reservation.released)


async def _active_reservations(
    session: AsyncSession,
    environment: str,
    kind: str,
    *,
    currency: str | None = None,
    symbol: str | None = None,
) -> list[Reservation]:
    stmt = select(Reservation).where(
        Reservation.environment == environment,
        Reservation.kind == kind,
        Reservation.status.in_(_ACTIVE),
    )
    if currency is not None:
        stmt = stmt.where(Reservation.currency == currency.upper())
    if symbol is not None:
        stmt = stmt.where(Reservation.symbol == symbol)
    return list(await session.scalars(stmt))


class ReservationManager:
    """所有写入由调用方提交；咨询锁保留到该事务结束。"""

    def __init__(self, environment: str) -> None:
        self.environment = environment

    async def budget_view(
        self, session: AsyncSession, account: AccountView, currency: str
    ) -> BudgetView:
        currency = currency.upper()
        if account.has_debt:
            raise ReservationError("ACCOUNT_HAS_DEBT", "账户任一币种存在负债，必须暂停")
        balance = account.coin(currency)
        if balance.wallet_balance is None:
            raise ReservationError(
                "CASH_UNVERIFIED", f"{currency} 缺少自有现金字段，权益/抵押品不能替代现金"
            )
        own_cash = dec(balance.wallet_balance)
        if any(
            not value.is_finite() or value < 0
            for value in (own_cash, balance.available, balance.locked)
        ):
            raise ReservationError("CASH_UNVERIFIED", f"{currency} 现金快照含无效金额")
        reservations = await _active_reservations(
            session, self.environment, "CASH", currency=currency
        )
        local_held = sum((_remaining(r) for r in reservations), ZERO)
        # 适配器必须证明快照已反映这些订单的冻结；不能仅凭发送/受理状态推断。
        reflected = set(account.raw.get("reflected_order_link_ids", []))
        overlap = ZERO
        if reservations and reflected:
            rows = list(
                await session.scalars(
                    select(Order).where(
                        Order.environment == self.environment,
                        Order.reservation_id.in_([r.id for r in reservations]),
                        Order.order_link_id.in_(reflected),
                        Order.status.in_(["ACCEPTED", "PARTIALLY_FILLED"]),
                        Order.sync_state == "CONFIRMED",
                    )
                )
            )
            by_reservation: dict[int, Decimal] = {}
            for order in rows:
                amount = max(ZERO, order.qty - order.qty_filled) * order.price
                by_reservation[order.reservation_id] = (
                    by_reservation.get(order.reservation_id, ZERO) + amount
                )
            overlap = sum(
                (min(_remaining(r), by_reservation.get(r.id, ZERO)) for r in reservations), ZERO
            )
            overlap = min(overlap, max(ZERO, balance.locked))
        pending_held = local_held - overlap
        exchange_available = dec(balance.available)
        usable = min(exchange_available - pending_held, own_cash - local_held)
        return BudgetView(
            currency, own_cash, exchange_available, local_held, pending_held, max(ZERO, usable)
        )

    async def reserve_cash(
        self,
        session: AsyncSession,
        *,
        intent_id: int,
        amount: Decimal,
        currency: str,
        account: AccountView,
    ) -> Reservation:
        amount = dec(amount)
        currency = currency.upper()
        if not amount.is_finite() or amount <= 0:
            raise ReservationError("INVALID_AMOUNT", "预留金额必须为有限正数")
        await _lock_cash(session, self.environment, currency)
        view = await self.budget_view(session, account, currency)
        if view.usable < amount:
            raise ReservationError(
                "INSUFFICIENT_CASH",
                f"{currency} 需要 {amount}，可用 {view.usable}；自有现金 {view.own_cash}，"
                f"交易所可用 {view.exchange_available}，本地占用 {view.local_held}",
            )
        reservation = Reservation(
            environment=self.environment,
            intent_id=intent_id,
            kind="CASH",
            currency=currency,
            amount=amount,
            consumed=ZERO,
            released=ZERO,
            status=ReservationStatus.HELD,
        )
        session.add(reservation)
        await session.flush()
        return reservation

    async def closeable_qty(
        self,
        session: AsyncSession,
        *,
        symbol: str,
        strategy_id: int | None,
        account_qty: Decimal,
        account_trusted: bool,
        account_available: Decimal | None = None,
        reflected_order_link_ids: tuple[str, ...] | list[str] = (),
    ) -> Decimal:
        if not account_trusted or not dec(account_qty).is_finite() or account_qty < 0:
            raise ReservationError("INVENTORY_UNTRUSTED", "账户持仓不可信，禁止新卖单")
        if strategy_id is None:
            raise ReservationError("STRATEGY_REQUIRED", "跨策略减仓必须有明确分摊授权")
        bot_qty = await self.bot_managed_qty(session, symbol)
        strategy_qty = await self.strategy_qty(session, symbol, strategy_id)
        pending_all = await self._pending_close_qty(session, symbol)
        pending_strategy = await self._pending_close_qty(session, symbol, strategy_id)
        exchange_limit = dec(account_qty) - pending_all
        if account_available is not None:
            account_available = dec(account_available)
            if not account_available.is_finite() or account_available < 0:
                raise ReservationError("INVENTORY_UNTRUSTED", "交易所可用库存金额无效")
            overlap = ZERO
            if reflected_order_link_ids:
                held = await _active_reservations(
                    session, self.environment, "INVENTORY", symbol=symbol
                )
                orders = list(
                    await session.scalars(
                        select(Order).where(
                            Order.environment == self.environment,
                            Order.symbol == symbol,
                            Order.side == "sell",
                            Order.order_link_id.in_(reflected_order_link_ids),
                            Order.status.in_(["ACCEPTED", "PARTIALLY_FILLED"]),
                            Order.sync_state == "CONFIRMED",
                        )
                    )
                )
                by_reservation: dict[int, Decimal] = {}
                for order in orders:
                    if order.reservation_id is not None:
                        by_reservation[order.reservation_id] = by_reservation.get(
                            order.reservation_id, ZERO
                        ) + max(ZERO, order.qty - order.qty_filled)
                overlap = sum(
                    (min(_remaining(row), by_reservation.get(row.id, ZERO)) for row in held), ZERO
                )
                overlap = min(overlap, max(ZERO, account_qty - account_available))
            exchange_limit = account_available - (pending_all - overlap)
        return max(
            ZERO,
            min(
                dec(account_qty) - pending_all,
                exchange_limit,
                bot_qty - pending_all,
                strategy_qty - pending_strategy,
            ),
        )

    async def bot_managed_qty(self, session: AsyncSession, symbol: str) -> Decimal:
        return dec(
            await session.scalar(
                select(func.coalesce(func.sum(Inventory.qty), 0)).where(
                    Inventory.environment == self.environment,
                    Inventory.symbol == symbol,
                    Inventory.trusted.is_(True),
                )
            )
        )

    async def strategy_qty(
        self, session: AsyncSession, symbol: str, strategy_id: int | None
    ) -> Decimal:
        if strategy_id is None:
            return ZERO
        return dec(
            await session.scalar(
                select(func.coalesce(func.sum(Inventory.qty), 0)).where(
                    Inventory.environment == self.environment,
                    Inventory.symbol == symbol,
                    Inventory.strategy_id == strategy_id,
                    Inventory.trusted.is_(True),
                )
            )
        )

    async def _pending_close_qty(
        self, session: AsyncSession, symbol: str, strategy_id: int | None = None
    ) -> Decimal:
        stmt = select(Reservation).where(
            Reservation.environment == self.environment,
            Reservation.kind == "INVENTORY",
            Reservation.symbol == symbol,
            Reservation.status.in_(_ACTIVE),
        )
        if strategy_id is not None:
            stmt = stmt.join(TradeIntent, Reservation.intent_id == TradeIntent.id).where(
                TradeIntent.strategy_id == strategy_id
            )
        return sum((_remaining(r) for r in await session.scalars(stmt)), ZERO)

    async def reserve_inventory(
        self,
        session: AsyncSession,
        *,
        intent_id: int,
        symbol: str,
        qty: Decimal,
        account_qty: Decimal,
        account_trusted: bool,
        strategy_id: int | None,
        account_available: Decimal | None = None,
        reflected_order_link_ids: tuple[str, ...] | list[str] = (),
    ) -> Reservation:
        qty = dec(qty)
        if not qty.is_finite() or qty <= 0:
            raise ReservationError("INVALID_AMOUNT", "预留数量必须为有限正数")
        await _lock_inventory(session, self.environment, symbol)
        available = await self.closeable_qty(
            session,
            symbol=symbol,
            strategy_id=strategy_id,
            account_qty=account_qty,
            account_trusted=account_trusted,
            account_available=account_available,
            reflected_order_link_ids=reflected_order_link_ids,
        )
        if available < qty:
            raise ReservationError(
                "INSUFFICIENT_INVENTORY", f"{symbol} 需要 {qty}，可平量 {available}"
            )
        reservation = Reservation(
            environment=self.environment,
            intent_id=intent_id,
            kind="INVENTORY",
            symbol=symbol,
            amount=qty,
            consumed=ZERO,
            released=ZERO,
            status=ReservationStatus.HELD,
        )
        session.add(reservation)
        await session.flush()
        return reservation

    async def _get(self, session: AsyncSession, reservation_id: int) -> Reservation:
        reservation = await session.scalar(
            select(Reservation)
            .where(Reservation.id == reservation_id, Reservation.environment == self.environment)
            .with_for_update()
        )
        if reservation is None:
            raise ReservationError("RESERVATION_NOT_FOUND", f"预留 {reservation_id} 不存在")
        return reservation

    async def consume(
        self,
        session: AsyncSession,
        reservation_id: int,
        amount: Decimal,
        *,
        allow_overrun: bool = False,
    ) -> Reservation:
        """实际成交可以把先前释放额度重新分类为消耗；不能拒绝晚到事实。"""
        reservation = await self._get(session, reservation_id)
        amount = dec(amount)
        if amount <= 0:
            return reservation
        if not allow_overrun and reservation.consumed + amount > reservation.amount:
            raise ReservationError("OVER_CONSUME", "消耗超过原始预留")
        reclaimed = min(reservation.released, max(ZERO, amount - _remaining(reservation)))
        reservation.released -= reclaimed
        reservation.consumed += amount
        reservation.status = (
            ReservationStatus.RELEASED
            if _remaining(reservation) == 0
            else ReservationStatus.PARTIALLY_CONSUMED
        )
        await session.flush()
        return reservation

    async def release(
        self, session: AsyncSession, reservation_id: int, amount: Decimal | None = None
    ) -> Reservation:
        """仅调用方已取得足够终态证据时释放；None 释放全部剩余额度。"""
        reservation = await self._get(session, reservation_id)
        remaining = _remaining(reservation)
        released = remaining if amount is None else min(max(ZERO, dec(amount)), remaining)
        reservation.released += released
        reservation.status = (
            ReservationStatus.RELEASED
            if _remaining(reservation) == 0
            else ReservationStatus.PARTIALLY_CONSUMED
        )
        await session.flush()
        return reservation

    async def bind_order(self, session: AsyncSession, reservation_id: int, order_id: int) -> None:
        reservation = await self._get(session, reservation_id)
        order = await session.get(Order, order_id)
        if (
            order is None
            or order.environment != self.environment
            or order.intent_id != reservation.intent_id
        ):
            raise ReservationError("ORDER_MISMATCH", "订单与预留不属于同一意图/环境")
        order.reservation_id = reservation_id
        await session.flush()


def expiry_guard(expires_at: dt.datetime, *, now: dt.datetime | None = None) -> bool:
    return (now or dt.datetime.now(dt.UTC)) >= expires_at


__all__ = ["BudgetView", "ReservationError", "ReservationManager", "expiry_guard"]
