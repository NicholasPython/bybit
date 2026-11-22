"""Durable pair execution against real isolated PostgreSQL and synthetic exchange facts."""

import asyncio
from dataclasses import replace
from decimal import Decimal as D

import pytest
from sqlalchemy import select

from bybitbot.adapters.base import InstrumentSpec, OrderBook, OrderUncertain
from bybitbot.clock import now_utc
from bybitbot.execution.executor import ExecutionAuthorityError
from bybitbot.service import DurablePaperAdapter, StateStore, TradingService
from bybitbot.settings.loader import load_config
from bybitbot.settings.models import SpreadSettings
from bybitbot.spread.execution import PairSignal
from bybitbot.spread.models import (
    SpreadExecution,
    SpreadLegOrder,
    SpreadPairTrade,
    SpreadReconciliationLog,
)
from bybitbot.spread.paper import LinearPaperAdapter
from bybitbot.spread.runner import SpreadExecutor


def settings():
    return SpreadSettings.model_validate(
        {
            "enabled": True,
            "pairs": [
                {
                    "pair_key": "BTCUSDT:ETHUSDT",
                    "symbol_a": "BTCUSDT",
                    "symbol_b": "ETHUSDT",
                    "hedge_ratio": "1",
                    "z_entry": "2",
                    "z_exit": "0.5",
                    "z_stop": "3.5",
                    "max_notional": {"value": "300", "currency": "USDT"},
                    "max_unhedged_notional": {"value": "20", "currency": "USDT"},
                    "recovery_window_ms": 500,
                    "enabled": True,
                }
            ],
        }
    )


def signal(**changes):
    args = {
        "pair_trade_id": "durable1",
        "pair_key": "BTCUSDT:ETHUSDT",
        "strategy_id": "spread",
        "symbol_a": "BTCUSDT",
        "symbol_b": "ETHUSDT",
        "direction": "LONG_A_SHORT_B",
        "notional": D("100"),
        "passive_price": D("100"),
        "active_price": D("100"),
        "qty_step_a": D("0.01"),
        "qty_step_b": D("0.01"),
        "recovery_window_ms": 500,
        "max_unhedged_notional": D("20"),
    }
    return PairSignal(**{**args, **changes})


def populate(adapter):
    for symbol in ("BTCUSDT", "ETHUSDT"):
        adapter.set_instrument(
            InstrumentSpec(
                "linear",
                symbol,
                symbol[:-4],
                "USDT",
                "Trading",
                settle_coin="USDT",
                tick_size=D("0.01"),
                qty_step=D("0.01"),
                min_order_qty=D("0.01"),
            )
        )
        book(adapter, symbol)
    return adapter


def book(adapter, symbol, *, bid="100", ask="100.10", size="10"):
    moment = now_utc()
    adapter.set_book(
        OrderBook(
            "linear",
            symbol,
            ((D(bid), D(size)),),
            ((D(ask), D(size)),),
            source_ts=moment,
            received_at=moment,
        )
    )


async def executor(database, adapter=None, *, recover=True):
    adapter = adapter or populate(LinearPaperAdapter(initial_balances={"USDT": D("1000")}))
    runner = SpreadExecutor(
        adapter=adapter,
        sessionmaker=database.sessionmaker,
        environment="paper",
        account_uid="paper-account",
        settings=settings(),
    )
    if recover:
        assert (await runner.recover()).allows_resume
        assert (await runner.tick()).allows_resume
    return runner


async def rows(database, model):
    async with database.sessionmaker() as session:
        return list(await session.scalars(select(model).order_by(model.id)))


async def open_hedged(database, runner):
    await runner.open_pair(signal())
    await runner.tick()
    book(runner.adapter, "BTCUSDT", bid="99.90", ask="100")
    assert (await runner.tick()).allows_resume
    book(runner.adapter, "ETHUSDT")
    assert (await runner.tick()).allows_resume
    assert (await rows(database, SpreadPairTrade))[0].state == "HEDGED"


async def test_pair_open_close_reconciles_pnl_fees_and_deduplicates(database):
    runner = await executor(database)
    await open_hedged(database, runner)
    pair = (await rows(database, SpreadPairTrade))[0]
    assert pair.trading_fee == D("0.11")
    assert await runner.open_pair(signal()) is not None
    assert len(await rows(database, SpreadLegOrder)) == 2
    with pytest.raises(ValueError, match="不同信号"):
        await runner.open_pair(signal(notional=D("101")))
    await runner.request_close("durable1", reason="converged")
    await runner.tick()
    book(runner.adapter, "BTCUSDT", bid="101", ask="101.10")
    book(runner.adapter, "ETHUSDT", bid="98.90", ask="99")
    # First protected close prices may no longer cross; IOC cancels, next tick reprices.
    await runner.tick()
    book(runner.adapter, "BTCUSDT", bid="101", ask="101.10")
    book(runner.adapter, "ETHUSDT", bid="98.90", ask="99")
    result = await runner.tick()
    assert result.allows_resume, result.summary()
    pair = (await rows(database, SpreadPairTrade))[0]
    assert pair.state == "CLOSED"
    assert pair.spread_pnl == D("2")
    assert pair.net_pnl == pair.spread_pnl - pair.trading_fee
    assert all(p.size == 0 for p in await runner.adapter.get_positions("linear"))
    wallet = (await runner.adapter.get_account()).coin("USDT").wallet_balance
    assert wallet == D("1000") + pair.net_pnl
    count = len(await rows(database, SpreadExecution))
    await runner.tick()
    assert len(await rows(database, SpreadExecution)) == count == 4
    assert (await rows(database, SpreadReconciliationLog))[-1].status == "CLEAN"


async def test_partial_ioc_only_replaces_the_confirmed_remaining_quantity(database):
    runner = await executor(database)
    await runner.open_pair(signal())
    await runner.tick()
    book(runner.adapter, "BTCUSDT", bid="99.90", ask="100")
    await runner.tick()
    book(runner.adapter, "ETHUSDT", size="0.40")
    await runner.tick()
    orders = await rows(database, SpreadLegOrder)
    hedges = [o for o in orders if o.leg == "B"]
    assert [o.qty for o in hedges] == [D("1"), D("0.6")]
    await runner.tick()
    assert len(await rows(database, SpreadLegOrder)) == 3
    book(runner.adapter, "ETHUSDT")
    await runner.tick()
    assert (await rows(database, SpreadPairTrade))[0].state == "HEDGED"
    assert (await rows(database, SpreadPairTrade))[0].filled_qty_b == D("1")


async def test_unfilled_passive_order_aborts_only_after_cancellation_evidence(database):
    runner = await executor(database)
    pair = await runner.open_pair(signal())
    await runner.tick()
    deadline = int(pair.opened_at.timestamp() * 1000) + 2000
    result = await runner.tick(now_ms=deadline)
    assert result.allows_resume
    order = (await rows(database, SpreadLegOrder))[0]
    pair = (await rows(database, SpreadPairTrade))[0]
    assert order.status == "CANCELED" and order.sync_state == "CONFIRMED"
    assert pair.state == "ABORTED" and pair.closed_at is not None
    assert not await rows(database, SpreadExecution)


async def test_claimed_send_missing_exchange_fact_is_never_replayed(database):
    runner = await executor(database)
    await runner.open_pair(signal())
    async with database.sessionmaker() as session:
        order = await session.scalar(select(SpreadLegOrder))
        order.status, order.sync_state, order.submitted_at = "UNKNOWN", "PENDING", now_utc()
        await session.commit()
    restarted = await executor(database, runner.adapter, recover=False)
    result = await restarted.recover()
    assert not result.allows_resume
    for _ in range(2):
        assert not (await restarted.tick()).allows_resume
    assert not await runner.adapter.get_order_history("linear")
    assert (await rows(database, SpreadLegOrder))[0].status == "UNKNOWN"
    with pytest.raises(ExecutionAuthorityError):
        await restarted.open_pair(signal(pair_trade_id="second"))


async def test_durable_exchange_acceptance_survives_lost_response_and_restart(database):
    store = StateStore(database, "paper")
    adapter = populate(DurablePaperAdapter(store, initial_balances={"USDT": D("1000")}))
    runner = await executor(database, adapter)
    await runner.open_pair(signal())
    adapter.inject_fault("after_send_timeout")
    order = (await rows(database, SpreadLegOrder))[0]
    await runner._send(order.id)
    assert (await rows(database, SpreadLegOrder))[0].status == "UNKNOWN"
    restored = DurablePaperAdapter(store)
    restored.restore_state(await store.get("paper"))
    populate(restored)
    restarted = await executor(database, restored, recover=False)
    assert (await restarted.recover()).allows_resume
    await restarted.tick()
    history = await restored.get_order_history("linear")
    assert len(history) == 1 and history[0].status == "Cancelled"
    assert (await rows(database, SpreadPairTrade))[0].state == "ABORTED"


async def test_restart_with_fill_creates_one_hedge_without_duplicate_entry(database):
    runner = await executor(database)
    await runner.open_pair(signal())
    await runner.tick()
    book(runner.adapter, "BTCUSDT", bid="99.90", ask="100")
    restarted = await executor(database, runner.adapter, recover=False)
    assert (await restarted.recover()).allows_resume
    await restarted.tick()
    book(runner.adapter, "ETHUSDT")
    await restarted.tick()
    assert len(await rows(database, SpreadLegOrder)) == 2
    assert len(await rows(database, SpreadExecution)) == 2
    assert (await rows(database, SpreadPairTrade))[0].state == "HEDGED"


async def test_missing_fill_stream_blocks_ahead_order_snapshot(database, monkeypatch):
    runner = await executor(database)
    await runner.open_pair(signal())
    await runner.tick()
    book(runner.adapter, "BTCUSDT", bid="99.90", ask="100")
    original = runner.adapter.get_executions

    async def missing(*args, **kwargs):
        return []

    monkeypatch.setattr(runner.adapter, "get_executions", missing)
    result = await runner.tick()
    assert not result.allows_resume
    assert len(await rows(database, SpreadLegOrder)) == 1
    monkeypatch.setattr(runner.adapter, "get_executions", original)
    assert (await runner.tick()).allows_resume
    assert len(await rows(database, SpreadLegOrder)) == 2


async def test_external_cash_or_position_change_blocks_without_absorbing_it(database):
    runner = await executor(database)
    runner.adapter._account.credit("USDT", D("1"))
    first = await runner.tick()
    second = await runner.tick()
    assert not first.allows_resume and not second.allows_resume
    assert any(d.kind == "LINEAR_CASH_DIFFERENCE" for d in second.differences)


async def test_execution_id_conflict_cannot_mutate_booked_fact(database, monkeypatch):
    runner = await executor(database)
    await open_hedged(database, runner)
    original = runner.adapter.get_executions

    async def conflicted(*args, **kwargs):
        fills = await original(*args, **kwargs)
        return [replace(f, exec_price=f.exec_price + D("1")) for f in fills]

    monkeypatch.setattr(runner.adapter, "get_executions", conflicted)
    result = await runner.tick()
    assert not result.allows_resume
    assert any(d.kind == "EXECUTION_ID_CONFLICT" for d in result.differences)
    assert {f.exec_price for f in await rows(database, SpreadExecution)} == {D("100")}


async def test_excessive_chase_slippage_flattens_attributed_position(database):
    runner = await executor(database)
    await runner.open_pair(signal())
    await runner.tick()
    book(runner.adapter, "BTCUSDT", bid="99.90", ask="100")
    book(runner.adapter, "ETHUSDT", bid="110", ask="110.10")
    await runner.tick()
    pair = (await rows(database, SpreadPairTrade))[0]
    assert pair.state == "ERROR_UNHEDGED"
    orders = await rows(database, SpreadLegOrder)
    assert not any(o.leg == "B" for o in orders)
    assert any(o.role == "FLATTEN" and o.qty == D("1") for o in orders)
    book(runner.adapter, "BTCUSDT", bid="99.90", ask="100")
    await runner.tick()
    pair = (await rows(database, SpreadPairTrade))[0]
    assert pair.state == "ERROR_UNHEDGED" and pair.closed_at is not None


async def test_pair_gross_notional_reservation_and_direction_are_enforced(database):
    runner = await executor(database)
    with pytest.raises(ValueError, match="方向"):
        await runner.open_pair(signal(direction="SHORT_A_LONG_B"))
    with pytest.raises(ExecutionAuthorityError, match="PAIR_EQUITY_RATIO"):
        await runner.open_pair(signal(notional=D("151")))
    await runner.open_pair(signal())
    with pytest.raises(ExecutionAuthorityError, match="共享"):
        await runner.open_pair(signal(pair_trade_id="overlap"))


class LinearMarket(LinearPaperAdapter):
    """Fresh deterministic market reads; fills still wait for a subsequent book."""

    def __init__(self):
        super().__init__()
        populate(self)
        self.fill_a = False

    async def get_ticker(self, category, symbol):
        book(
            self,
            symbol,
            bid="99.90" if symbol == "BTCUSDT" and self.fill_a else "100",
            ask="100" if symbol == "BTCUSDT" and self.fill_a else "100.10",
        )
        return await super().get_ticker(category, symbol)

    async def get_orderbook(self, category, symbol, limit=50):
        book(
            self,
            symbol,
            bid="99.90" if symbol == "BTCUSDT" and self.fill_a else "100",
            ask="100" if symbol == "BTCUSDT" and self.fill_a else "100.10",
        )
        return await super().get_orderbook(category, symbol, limit)


async def test_worker_spread_mode_baseline_takeover_trade_and_restart(database):
    from bybitbot.execution.lease import LeaseState
    from bybitbot.ops.control import CommandKind

    async def command(service, kind, payload):
        import uuid

        await service.commands.submit(
            command_id=str(uuid.uuid4()), kind=kind, payload=payload, requested_by="test-operator"
        )
        await service._process_commands()

    config = load_config("config/config.example.yaml").model_copy(update={"spread": settings()})
    market = LinearMarket()
    service = TradingService(config, database, market_adapter=market)
    try:
        await service.start()
        assert service.identity_ok and service.reconcile_ok
        assert service.lease.state is LeaseState.NEEDS_CONFIRMATION
        await command(service, CommandKind.INITIALIZE_BASELINE, {"reason": "spread paper"})
        await command(
            service,
            CommandKind.CONFIRM_TAKEOVER,
            {"reason": "spread paper", "evidence": "isolated old executor stopped"},
        )
        await service.tick()
        assert service.baseline_ok and not await service.pauses.active_pauses()
        await service.open_spread(signal())
        await service.tick()
        market.fill_a = True
        await service.tick()
        await service.tick()
        assert (await rows(database, SpreadPairTrade))[0].state == "HEDGED"
        assert service.valuation.complete
        wallet = service.account.coin("USDT").wallet_balance
        assert service.valuation.equity != wallet  # Includes linear UPL, not position market value.
    finally:
        await service.close()
    restarted = TradingService(config, database, market_adapter=market)
    try:
        await restarted.start()
        assert restarted.reconcile_ok and restarted.baseline_ok
        assert restarted.lease.state is LeaseState.NEEDS_CONFIRMATION
        assert len(await rows(database, SpreadLegOrder)) == 2
        with pytest.raises(RuntimeError, match="执行权"):
            await restarted.open_spread(signal(pair_trade_id="blocked"))
    finally:
        await restarted.close()


async def test_cancel_fill_race_accounts_fill_and_hedges_once(database, monkeypatch):
    runner = await executor(database)
    pair = await runner.open_pair(signal())
    await runner.tick()
    original = runner.adapter.cancel_order

    async def racing_cancel(*args, **kwargs):
        book(runner.adapter, "BTCUSDT", bid="99.90", ask="100")
        await original(*args, **kwargs)
        raise OrderUncertain("cancellation response lost after racing fill")

    monkeypatch.setattr(runner.adapter, "cancel_order", racing_cancel)
    result = await runner.tick(now_ms=int(pair.opened_at.timestamp() * 1000) + 2000)
    assert result.allows_resume, result.summary()
    assert (await rows(database, SpreadLegOrder))[0].status == "FILLED"
    assert len([o for o in await rows(database, SpreadLegOrder) if o.leg == "B"]) == 1
    book(runner.adapter, "ETHUSDT")
    await runner.tick()
    assert (await rows(database, SpreadPairTrade))[0].state == "HEDGED"


async def test_late_fill_preserves_terminal_state_but_recovers_exit_monitoring(database):
    runner = await executor(database)
    pair = await runner.open_pair(signal())
    await runner.tick()
    await runner.tick(now_ms=int(pair.opened_at.timestamp() * 1000) + 2000)
    assert (await rows(database, SpreadPairTrade))[0].state == "ABORTED"
    physical = next(iter(runner.adapter._orders.values()))
    runner.adapter._apply_fill(physical, D("100"), D("1"))
    await runner.tick()
    pair = (await rows(database, SpreadPairTrade))[0]
    assert pair.state == "ABORTED" and pair.closed_at is None
    assert any(o.role == "FLATTEN" for o in await rows(database, SpreadLegOrder))
    book(runner.adapter, "BTCUSDT")
    await runner.tick()
    pair = (await rows(database, SpreadPairTrade))[0]
    assert pair.state == "ABORTED" and pair.closed_at is not None


async def test_lease_loss_between_claim_and_io_keeps_unknown_without_sending(database):
    from types import SimpleNamespace

    from bybitbot.execution.lease import LeaseState

    runner = await executor(database)
    await runner.open_pair(signal())
    order = (await rows(database, SpreadLegOrder))[0]

    class LostLease:
        environment = "paper"
        account_uid = "paper-account"
        calls = 0

        async def heartbeat(self):
            self.calls += 1
            return SimpleNamespace(
                state=LeaseState.ELIGIBLE if self.calls == 1 else LeaseState.LOST
            )

    runner.lease = LostLease()
    await runner._send(order.id)
    row = (await rows(database, SpreadLegOrder))[0]
    assert row.status == "UNKNOWN" and row.submitted_at is not None
    assert not await runner.adapter.get_order_history("linear")


async def test_cancelled_send_task_leaves_durable_unknown_for_recovery(database, monkeypatch):
    runner = await executor(database)
    await runner.open_pair(signal())
    order = (await rows(database, SpreadLegOrder))[0]
    original = runner.adapter.create_order

    async def cancelled(request):
        await original(request)
        raise asyncio.CancelledError()

    monkeypatch.setattr(runner.adapter, "create_order", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await runner._send(order.id)
    assert (await rows(database, SpreadLegOrder))[0].sync_state == "UNCERTAIN"
    result = await runner.reconciler.reconcile()
    assert result.allows_resume
    assert (await rows(database, SpreadLegOrder))[0].status == "WORKING"
    assert len(await runner.adapter.get_order_history("linear")) == 1


async def test_external_position_is_detected_and_not_attributed(database, monkeypatch):
    from bybitbot.adapters.base import PositionView

    runner = await executor(database)

    async def external(category):
        return [PositionView("linear", "BTCUSDT", size=D("1"), side="Buy", settle_coin="USDT")]

    monkeypatch.setattr(runner.adapter, "get_positions", external)
    result = await runner.tick()
    assert not result.allows_resume
    assert any(d.kind == "LINEAR_POSITION_DIFFERENCE" for d in result.differences)
    assert not await rows(database, SpreadPairTrade)


async def test_failed_calibration_dimension_stops_actions(database, monkeypatch):
    runner = await executor(database)
    await runner.open_pair(signal())

    async def failed(category):
        raise TimeoutError("position query failed")

    monkeypatch.setattr(runner.adapter, "get_positions", failed)
    result = await runner.tick()
    assert not result.allows_resume
    assert not await runner.adapter.get_order_history("linear")


async def test_durable_rate_budget_blocks_unclaimed_send(database):
    runner = await executor(database)
    await runner.open_pair(signal())
    async with database.sessionmaker() as session:
        pair = await session.scalar(select(SpreadPairTrade))
        pair.context = {**pair.context, "write_events_ms": [int(now_utc().timestamp() * 1000)] * 15}
        await session.commit()
    order = (await rows(database, SpreadLegOrder))[0]
    with pytest.raises(ExecutionAuthorityError, match="15"):
        await runner._send(order.id)
    assert (await rows(database, SpreadLegOrder))[0].submitted_at is None
    assert not await runner.adapter.get_order_history("linear")


async def test_missing_paper_checkpoint_cannot_reset_existing_spread_cash(database):
    runner = await executor(database)
    await runner.open_pair(signal())
    service = TradingService(
        load_config("config/config.example.yaml"), database, market_adapter=LinearMarket()
    )
    with pytest.raises(RuntimeError, match="检查点缺失"):
        await service._adapters()
