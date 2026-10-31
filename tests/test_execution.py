from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal as D

import pytest
from sqlalchemy import func, select, text

from bybitbot.adapters.base import (
    CancelAck,
    ExecutionView,
    OrderAck,
    OrderRejected,
    OrderView,
    PositionView,
)
from bybitbot.adapters.paper import PaperAdapter
from bybitbot.clock import now_utc
from bybitbot.db.models import (
    AccountSnapshot,
    CashFlow,
    Execution,
    Instrument,
    Inventory,
    LedgerEvent,
    Order,
    Position,
    ReconciliationRun,
    Reservation,
    RiskPause,
    Strategy,
    TradeIntent,
)
from bybitbot.db.models import ExecutorLease as LeaseRow
from bybitbot.execution.executor import ExecutionAuthorityError, Executor, OrderPlan
from bybitbot.execution.lease import ExecutorLease, LeaseState
from bybitbot.execution.states import (
    CANCELED,
    FILLED,
    PARTIALLY_FILLED,
    SYNC_CONFIRMED,
    SYNC_UNCERTAIN,
    OrderState,
    TransitionError,
    apply_transition,
)
from bybitbot.ledger import LedgerWriter
from bybitbot.reconciliation import Reconciler


class ControlledPaper(PaperAdapter):
    def __init__(self, sessionmaker, *, error=None):
        super().__init__(initial_balances={"USDT": D("1000")})
        self.sessionmaker = sessionmaker
        self.calls = 0
        self.error = error
        self.history = []
        self.fills = []
        self.actual_positions = []
        self.active_orders = []
        self.windows = []

    async def create_order(self, request):
        self.calls += 1
        async with self.sessionmaker() as session:
            row = await session.scalar(
                select(Order).where(Order.order_link_id == request.order_link_id)
            )
            assert row.status == "SUBMITTING" and row.sync_state == "PENDING"
            assert await session.get(Reservation, row.reservation_id)
        if self.error:
            raise self.error
        return OrderAck("exchange-order", request.order_link_id, "New")

    async def cancel_order(self, *args, **kwargs):
        return CancelAck("exchange-order", kwargs.get("order_link_id", ""), True)

    async def get_order_history(self, category, **kwargs):
        return [view for view in self.history if view.category == category]

    async def get_executions(
        self, category, *, order_link_ids=None, start_time=None, end_time=None
    ):
        self.windows.append((start_time, end_time))
        return [view for view in self.fills if view.category == category]

    async def get_open_orders(self, category):
        return [view for view in self.active_orders if view.category == category]

    async def get_positions(self, category):
        return [view for view in self.actual_positions if view.category == category]


async def prepared(
    sessionmaker,
    *,
    category="spot",
    side="buy",
    qty="1",
    price="10",
    status="APPROVED",
    created_at=None,
):
    async with sessionmaker() as session:
        strategy = Strategy(environment="paper", name="exec-" + uuid.uuid4().hex[:8], kind="test")
        session.add(strategy)
        await session.flush()
        symbol = "BTCUSDT" if category == "spot" else "BTC-30NOV26-100-C-USDT"
        intent = TradeIntent(
            environment="paper",
            account_uid="paper-account",
            strategy_id=strategy.id,
            strategy_version=1,
            signal_id=uuid.uuid4().hex,
            action="SPOT_BUY" if category == "spot" else "OPTION_BUY_OPEN",
            category=category,
            symbol=symbol,
            side=side,
            qty=D(qty),
            qty_unit="BTC",
            price_limit=D(price),
            spend_limit=D(qty) * D(price),
            currency="USDT",
            expires_at=now_utc() + dt.timedelta(minutes=1),
            status=status,
            created_at=created_at or now_utc(),
        )
        session.add(intent)
        session.add(
            Instrument(
                environment="paper",
                category=category,
                symbol=symbol,
                base_coin="BTC",
                quote_coin="USDT",
                settle_coin="USDT",
                status="Trading",
                tick_size=D(".01"),
                qty_step=D(".001"),
            )
        )
        await session.flush()
        reservation = Reservation(
            environment="paper",
            intent_id=intent.id,
            kind="CASH",
            currency="USDT",
            amount=D(qty) * D(price) + D("1"),
            consumed=D("0"),
            released=D("0"),
            status="HELD",
        )
        session.add(reservation)
        await session.commit()
        return intent, reservation, strategy.id


def make_executor(sessionmaker, adapter):
    return Executor(
        adapter=adapter,
        sessionmaker=sessionmaker,
        environment="paper",
        account_uid="paper-account",
        cancel_verify_timeout=0,
        cancel_poll_interval=0,
    )


def plan(intent, reservation):
    return OrderPlan(
        intent.id,
        intent.category,
        intent.symbol,
        intent.side,
        qty=intent.qty,
        price=intent.price_limit,
        reservation_id=reservation.id,
        spend_limit=intent.spend_limit,
    )


def view(order, *, filled="0", status="New"):
    return OrderView(
        "exchange-order",
        order.order_link_id,
        order.category,
        order.symbol,
        order.side,
        "Limit",
        status,
        order.price,
        order.qty,
        D(filled),
    )


def fill(order, *, identifier="exec-1", qty="1", fee="0", asset="USDT", at=None):
    return ExecutionView(
        identifier,
        "exchange-order",
        order.order_link_id,
        order.category,
        order.symbol,
        order.side,
        order.price,
        D(qty),
        at or now_utc(),
        D(fee),
        asset,
    )


async def test_unknown_never_retries_or_releases_missing_query(sessionmaker):
    intent, reservation, _ = await prepared(sessionmaker)
    adapter = ControlledPaper(sessionmaker, error=TimeoutError("response lost"))
    executor = make_executor(sessionmaker, adapter)
    first = await executor.submit(plan(intent, reservation))
    second = await executor.submit(plan(intent, reservation))
    assert first.is_unknown and second.is_unknown and adapter.calls == 1
    assert not (await executor.resolve_unknown(first.order)).resolved
    async with sessionmaker() as session:
        row = await session.get(Reservation, reservation.id)
        assert row.consumed == 0 and row.released == 0
        assert await session.scalar(
            select(RiskPause.active).where(RiskPause.reason_code == "UNKNOWN_ORDER")
        )


@pytest.mark.parametrize("code,unknown", [(110072, True), (170141, True), (110004, False)])
async def test_duplicate_id_is_not_rejection_evidence(sessionmaker, code, unknown):
    intent, reservation, _ = await prepared(sessionmaker)
    adapter = ControlledPaper(sessionmaker, error=OrderRejected("rejected", ret_code=code))
    result = await make_executor(sessionmaker, adapter).submit(plan(intent, reservation))
    assert result.is_unknown == unknown
    async with sessionmaker() as session:
        row = await session.get(Reservation, reservation.id)
        assert (row.released == row.amount) == (not unknown)


async def test_terminal_before_fill_preserves_reservation_and_no_double_count(sessionmaker):
    intent, reservation, strategy = await prepared(sessionmaker)
    adapter = ControlledPaper(sessionmaker)
    executor = make_executor(sessionmaker, adapter)
    result = await executor.submit(plan(intent, reservation))
    terminal = await executor.apply_order_view(view(result.order, filled=".4", status="Cancelled"))
    assert not terminal.resolved
    async with sessionmaker() as session:
        assert (await session.get(Reservation, reservation.id)).released == 0
    execution = fill(result.order, qty=".4", fee=".01")
    assert await executor.apply_execution(execution)
    assert not await executor.apply_execution(execution)
    await executor.apply_order_view(view(result.order, filled="0", status="New"))
    async with sessionmaker() as session:
        row = await session.get(Order, result.order.id)
        held = await session.get(Reservation, reservation.id)
        inventory = await session.scalar(select(Inventory).where(Inventory.strategy_id == strategy))
        assert row.status == CANCELED and row.qty_filled == D(".4")
        assert row.sync_state == SYNC_CONFIRMED
        assert held.consumed == D("4.01") and held.released == D("6.99")
        assert inventory.qty == D(".4")
        assert await session.scalar(select(func.count()).select_from(Execution)) == 1


async def test_cancel_ack_without_terminal_preserves_held(sessionmaker):
    intent, reservation, _ = await prepared(sessionmaker)
    adapter = ControlledPaper(sessionmaker)
    executor = make_executor(sessionmaker, adapter)
    result = await executor.submit(plan(intent, reservation))
    assert not (await executor.cancel(result.order)).resolved
    async with sessionmaker() as session:
        assert (await session.get(Reservation, reservation.id)).released == 0
        assert (await session.get(Order, result.order.id)).status == "ACCEPTED"


async def test_late_fill_after_cancel_reclassifies_release_and_upgrades_full(sessionmaker):
    intent, reservation, _ = await prepared(sessionmaker)
    executor = make_executor(sessionmaker, ControlledPaper(sessionmaker))
    result = await executor.submit(plan(intent, reservation))
    assert (await executor.apply_order_view(view(result.order, status="Cancelled"))).resolved
    assert await executor.apply_execution(fill(result.order, fee=".1"))
    async with sessionmaker() as session:
        row = await session.get(Order, result.order.id)
        held = await session.get(Reservation, reservation.id)
        assert row.status == FILLED and row.qty_filled == 1
        assert held.consumed == D("10.1") and held.released == D(".9")


async def test_reject_full_status_without_cumulative_evidence(sessionmaker):
    intent, reservation, _ = await prepared(sessionmaker)
    executor = make_executor(sessionmaker, ControlledPaper(sessionmaker))
    result = await executor.submit(plan(intent, reservation))
    assert not (await executor.apply_order_view(view(result.order, status="Filled"))).resolved
    async with sessionmaker() as session:
        assert (await session.get(Reservation, reservation.id)).released == 0


async def test_recovery_orphan_reservation_proves_unsent_and_cannot_revive(sessionmaker):
    intent, reservation, _ = await prepared(sessionmaker)
    adapter = ControlledPaper(sessionmaker)
    executor = make_executor(sessionmaker, adapter)
    reconciler = Reconciler(
        adapter=adapter,
        sessionmaker=sessionmaker,
        environment="paper",
        account_uid="paper-account",
        executor=executor,
    )
    assert (await reconciler.reconcile_reservations()).allows_resume
    with pytest.raises(ExecutionAuthorityError, match="恢复流程终止"):
        await executor.submit(plan(intent, reservation))
    async with sessionmaker() as session:
        assert (await session.get(Reservation, reservation.id)).released == reservation.amount
        assert await session.scalar(select(func.count()).select_from(LedgerEvent)) == 1
    assert adapter.calls == 0


async def test_prepared_unsent_recovery_releases_cash_units(sessionmaker):
    intent, reservation, _ = await prepared(sessionmaker)
    async with sessionmaker() as session:
        order = Order(
            environment="paper",
            intent_id=intent.id,
            reservation_id=reservation.id,
            order_link_id="unsent",
            category="spot",
            symbol=intent.symbol,
            side="buy",
            order_type="Limit",
            time_in_force="GTC",
            price=D("10"),
            qty=D("1"),
            qty_filled=D("0"),
            status="PREPARED",
            sync_state="NONE",
        )
        session.add(order)
        await session.commit()
    executor = make_executor(sessionmaker, ControlledPaper(sessionmaker))
    result = await executor.resolve_unknown(order)
    assert result.resolved
    async with sessionmaker() as session:
        held = await session.get(Reservation, reservation.id)
        assert held.released == D("11")
        assert (await session.get(Order, order.id)).sync_state == SYNC_CONFIRMED


async def test_reconciliation_retains_offline_fill_and_watermark(sessionmaker):
    old = now_utc() - dt.timedelta(days=8)
    intent, reservation, _ = await prepared(sessionmaker, created_at=old)
    adapter = ControlledPaper(sessionmaker)
    executor = make_executor(sessionmaker, adapter)
    result = await executor.submit(plan(intent, reservation))
    adapter.fills = [fill(result.order, at=now_utc() - dt.timedelta(days=7))]
    reconciler = Reconciler(
        adapter=adapter,
        sessionmaker=sessionmaker,
        environment="paper",
        account_uid="paper-account",
        executor=executor,
    )
    first = await reconciler.reconcile_executions()
    assert first.repaired and first.allows_resume
    assert adapter.windows[0][0] < old
    second = await reconciler.reconcile_executions()
    assert second.allows_resume and not second.repaired
    assert adapter.windows[-1][0] > old
    async with sessionmaker() as session:
        assert await session.scalar(select(func.count()).select_from(Execution)) == 1
        assert await session.scalar(select(func.count()).select_from(ReconciliationRun)) == 2


async def test_missing_position_locks_inventory_and_keeps_cost(sessionmaker):
    intent, reservation, strategy = await prepared(sessionmaker, category="option")
    adapter = ControlledPaper(sessionmaker)
    executor = make_executor(sessionmaker, adapter)
    result = await executor.submit(plan(intent, reservation))
    await executor.apply_execution(fill(result.order))
    adapter.actual_positions = [PositionView("option", intent.symbol, size=D("1"), side="Buy")]
    reconciler = Reconciler(
        adapter=adapter,
        sessionmaker=sessionmaker,
        environment="paper",
        account_uid="paper-account",
        executor=executor,
    )
    assert (await reconciler.reconcile_positions()).allows_resume
    adapter.actual_positions = []
    assert not (await reconciler.reconcile_positions()).allows_resume
    async with sessionmaker() as session:
        inventory = await session.scalar(select(Inventory).where(Inventory.strategy_id == strategy))
        position = await session.scalar(select(Position).where(Position.category == "option"))
        assert inventory.qty == 1 and inventory.avg_cost == 10 and not inventory.trusted
        assert position.size == 0 and not position.trusted


async def test_external_fill_is_recorded_but_never_owned(sessionmaker):
    adapter = ControlledPaper(sessionmaker)
    executor = make_executor(sessionmaker, adapter)
    external = ExecutionView(
        "external", "external-order", None, "option", "BTC-X", "Buy", D("10"), D("1"), now_utc()
    )
    assert await executor.apply_execution(external)
    async with sessionmaker() as session:
        assert await session.scalar(select(func.count()).select_from(Inventory)) == 0
        assert await session.scalar(
            select(RiskPause.active).where(RiskPause.reason_code == "EXTERNAL_ACCOUNT_CHANGE")
        )


async def test_external_cash_change_cannot_be_clean_recon(sessionmaker):
    adapter = ControlledPaper(sessionmaker)
    async with sessionmaker() as session:
        session.add(
            AccountSnapshot(
                environment="paper",
                account_uid="paper-account",
                account_mode="unified",
                coins={"USDT": {"wallet_balance": "900"}},
                complete=True,
            )
        )
        await session.commit()
    reconciler = Reconciler(
        adapter=adapter, sessionmaker=sessionmaker, environment="paper", account_uid="paper-account"
    )
    result = await reconciler.reconcile_cash()
    assert not result.allows_resume
    assert any(item.kind == "EXTERNAL_CASH_CHANGE" for item in result.differences)


async def test_lease_single_holder_and_no_auto_takeover(database, sessionmaker):
    uid = "lease-test-" + uuid.uuid4().hex
    first = ExecutorLease(database.engine, environment="paper", account_uid=uid)
    second = ExecutorLease(database.engine, environment="paper", account_uid=uid)
    try:
        assert (await first.try_acquire()).state == LeaseState.NEEDS_CONFIRMATION
        assert (await second.try_acquire()).state == LeaseState.HELD_BY_OTHER
        await first.heartbeat()
        await first.heartbeat()
        async with sessionmaker() as session:
            rows = list(await session.scalars(select(LeaseRow).where(LeaseRow.account_uid == uid)))
            assert len(rows) == 1
            assert rows[0].acquired_at <= rows[0].heartbeat_at
        assert (
            await first.confirm_takeover(operator="test", evidence="old process stopped")
        ).state == LeaseState.ELIGIBLE
        await first.release()
        assert (await second.try_acquire()).state == LeaseState.NEEDS_CONFIRMATION
        async with second._connection.begin_nested():
            await second._connection.execute(text("SELECT pg_advisory_unlock_all()"))
        assert (await second.heartbeat()).state == LeaseState.LOST
    finally:
        await first.release()
        await second.release()


async def test_remote_adapter_cannot_use_paper_label_to_bypass_lease(sessionmaker):
    class FakeRemote:
        environment = "paper"
        allow_trade_writes = True

    executor = make_executor(sessionmaker, FakeRemote())
    with pytest.raises(ExecutionAuthorityError, match="租约"):
        await executor._require_authority()


@pytest.mark.parametrize("payout", ["0", "20"])
async def test_settlement_evidence_idempotency_and_cost_release(sessionmaker, payout):
    intent, _, strategy = await prepared(sessionmaker, category="option")
    moment = now_utc()
    async with sessionmaker() as session:
        spec = await session.scalar(select(Instrument).where(Instrument.category == "option"))
        spec.delivery_time = moment - dt.timedelta(minutes=1)
        session.add(
            Inventory(
                environment="paper",
                strategy_id=strategy,
                symbol=intent.symbol,
                qty=D("1"),
                avg_cost=D("10"),
                currency="USDT",
                trusted=False,
                cost_source="bot_fill",
            )
        )
        await session.commit()
    ledger = LedgerWriter("paper")
    arguments = {
        "settlement_id": "delivery-1",
        "symbol": intent.symbol,
        "currency": "USDT",
        "occurred_at": moment,
        "allocations": [{"strategy_id": strategy, "qty": "1", "payout": payout, "fee": "0"}],
        "evidence": {
            "source": "official_fixture",
            "reference": "fixed_delivery_case",
            "balance_reconciled": True,
            "positions_reconciled": True,
        },
    }
    async with sessionmaker() as session:
        with pytest.raises(ValueError, match="完整余额"):
            await ledger.record_option_settlement(session, **{**arguments, "evidence": {}})
        assert (await session.scalar(select(Inventory))).qty == 1
        event = await ledger.record_option_settlement(session, **arguments)
        assert event.event_type == "SETTLEMENT"
        assert event.payload["allocations"][0]["released_cost"] == "10.000000000000000000"
        await session.commit()
    async with sessionmaker() as session:
        assert await ledger.record_option_settlement(session, **arguments) is None
        assert (await session.scalar(select(Inventory))).qty == 0
        assert await session.scalar(select(func.count()).select_from(LedgerEvent)) == 1
        assert await session.scalar(select(func.count()).select_from(Execution)) == 0
        flow = await session.scalar(select(CashFlow))
        assert flow.amount == D(payout) and not flow.external
        with pytest.raises(ValueError, match="事实冲突"):
            await ledger.record_option_settlement(
                session,
                **{
                    **arguments,
                    "allocations": [
                        {"strategy_id": strategy, "qty": "1", "payout": "99", "fee": "0"}
                    ],
                },
            )


def test_terminal_and_fill_monotonic_state():
    state = OrderState(CANCELED, SYNC_CONFIRMED, D(".4"))
    assert apply_transition(state, target="ACCEPTED", qty_filled=D(".2"), confirmed=True) == state
    updated = apply_transition(state, target=FILLED, qty_filled=D("1"), confirmed=True)
    assert updated.status == FILLED and updated.qty_filled == 1
    partial = apply_transition(
        OrderState(PARTIALLY_FILLED, SYNC_UNCERTAIN, D(".2")),
        target="ACCEPTED",
        qty_filled=D("0"),
        confirmed=True,
    )
    assert partial.status == PARTIALLY_FILLED and partial.qty_filled == D(".2")
    with pytest.raises(TransitionError):
        apply_transition(state, target="EXPIRED", confirmed=False)
