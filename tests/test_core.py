from __future__ import annotations

import asyncio
import datetime as dt
from dataclasses import replace
from decimal import Decimal as D

import pytest
from sqlalchemy import func, select

from bybitbot.adapters.base import AccountView, CoinBalance, Ticker
from bybitbot.clock import now_utc
from bybitbot.db.models import Inventory, Order, ScheduleRun, Strategy, TradeIntent
from bybitbot.ledger import LedgerWriter, order_link_id_for
from bybitbot.risk.delta import DeltaCandidate, evaluate_delta_bounds
from bybitbot.risk.engine import OpenOrderExposure, RiskContext, RiskEngine
from bybitbot.risk.reservation import ReservationError, ReservationManager
from bybitbot.scheduler import Scheduler
from bybitbot.settings.loader import load_config


def account(cash="100", available=None, locked="0", reflected=()):
    return AccountView(
        "paper-account",
        "unified",
        {"USDT": CoinBalance("USDT", D(cash), D(available or cash), D(cash), locked=D(locked))},
        raw={"reflected_order_link_ids": list(reflected)},
    )


def intent(**kwargs):
    values = {
        "environment": "paper",
        "account_uid": "paper-account",
        "signal_id": "core-test",
        "action": "SPOT_BUY",
        "category": "spot",
        "symbol": "BTCUSDT",
        "side": "buy",
        "qty": D(".1"),
        "qty_unit": "BTC",
        "price_limit": D("100"),
        "spend_limit": D("10"),
        "currency": "USDT",
        "expires_at": now_utc() + dt.timedelta(minutes=1),
        "status": "APPROVED",
    }
    values.update(kwargs)
    return TradeIntent(**values)


async def seed(sessionmaker):
    async with sessionmaker() as session:
        strategy = Strategy(environment="paper", name="test", kind="spot_fixed_budget_dca")
        session.add(strategy)
        await session.flush()
        row = intent(strategy_id=strategy.id)
        session.add(row)
        await session.commit()
        return strategy.id, row.id


def context(**kwargs):
    moment = now_utc()
    values = {
        "account": account(),
        "positions": [],
        "quotes": {
            "BTCUSDT": Ticker(
                "spot",
                "BTCUSDT",
                bid_price=D("99.9"),
                ask_price=D("100"),
                source_ts=moment,
                received_at=moment,
            )
        },
        "open_orders": [],
        "valuation_trusted": True,
        "identity_trusted": True,
        "account_delta_per_underlying": {"BTC": D("0")},
    }
    values.update(kwargs)
    return RiskContext(**values)


def test_opposite_orders_cannot_offset():
    check = evaluate_delta_bounds(
        underlying="BTC",
        d0=D("0"),
        limit=D("1"),
        candidates=[
            DeltaCandidate("a", "BTC", D("2"), D("2")),
            DeltaCandidate("b", "BTC", D("2"), D("-2")),
        ],
    )
    assert not check.approved
    assert (check.d_min, check.d_max) == (D("-2"), D("2"))


@pytest.mark.parametrize("unknown", [None, D("NaN"), D("Infinity")])
def test_unquantified_delta_rejected(unknown):
    result = evaluate_delta_bounds(
        underlying="BTC",
        d0=D("0"),
        limit=D("1"),
        candidates=[
            DeltaCandidate("x", "BTC", D("1"), unknown),
        ],
    )
    assert not result.approved
    assert result.reason_code == "DELTA_UNQUANTIFIED"


async def test_risk_requires_identity_valuation_and_authority():
    engine = RiskEngine(load_config("config/config.example.yaml"))
    assert (await engine.evaluate(intent(), context())).approved
    for name in ("identity_trusted", "valuation_trusted", "execution_authority"):
        result = await engine.evaluate(intent(), context(**{name: False}))
        assert not result.approved, name
        assert result.config_version
    assert not (await engine.evaluate(intent(), context(unknown_order_count=1))).approved
    assert not (await engine.evaluate(intent(symbol="SOLUSDT"), context())).approved


async def test_risk_uses_cash_and_two_sided_per_base_delta():
    engine = RiskEngine(load_config("config/config.example.yaml"))
    ctx = context(
        account_delta_per_underlying={"BTC": D("-.95"), "ETH": D("100")},
        open_orders=[
            OpenOrderExposure(1, "BTCUSDT", "sell", D(".1"), D("-.1")),
        ],
    )
    check = await engine.evaluate(intent(), ctx)
    assert not check.approved
    assert any(item.code == "DELTA_BOUNDS" and not item.approved for item in check.checks)
    stale = replace(ctx.quotes["BTCUSDT"], source_ts=now_utc() - dt.timedelta(hours=1))
    assert not (await engine.evaluate(intent(), context(quotes={"BTCUSDT": stale}))).approved


async def test_loss_pause_does_not_block_trusted_reduce():
    engine = RiskEngine(load_config("config/config.example.yaml"))
    row = intent(action="SPOT_SELL", side="sell", price_limit=D("99.9"), spend_limit=D("0"))
    ctx = context(
        daily_pnl=D("999"),
        drawdown=D("999"),
        valuation_trusted=False,
        account_pause_reasons=["DAILY_LOSS_LIMIT"],
    )
    assert (await engine.evaluate(row, ctx)).approved
    assert not (await engine.evaluate(row, replace(ctx, identity_trusted=False))).approved


async def test_missing_option_greeks_never_zero_delta():
    engine = RiskEngine(load_config("config/config.example.yaml"))
    row = intent(
        action="OPTION_BUY_OPEN",
        category="option",
        symbol="BTC-30NOV26-100-C-USDT",
        underlying="BTC",
        delivery_time=now_utc() + dt.timedelta(days=7),
    )
    check = engine._check_delta(row, context())
    assert not check.approved
    assert check.reason_code == "DELTA_UNQUANTIFIED"


async def test_cash_reservation_competition(sessionmaker):
    _, first = await seed(sessionmaker)
    async with sessionmaker() as session:
        other = intent(signal_id="second")
        session.add(other)
        await session.commit()
        second = other.id
    manager = ReservationManager("paper")

    async def reserve(identifier):
        async with sessionmaker() as session:
            try:
                row = await manager.reserve_cash(
                    session,
                    intent_id=identifier,
                    amount=D("60"),
                    currency="USDT",
                    account=account(),
                )
                await session.commit()
                return row.id
            except ReservationError:
                return None

    results = await asyncio.gather(reserve(first), reserve(second))
    assert sum(result is not None for result in results) == 1


async def test_cash_freeze_overlap_requires_order_evidence(sessionmaker):
    _, identifier = await seed(sessionmaker)
    manager = ReservationManager("paper")
    async with sessionmaker() as session:
        reservation = await manager.reserve_cash(
            session, intent_id=identifier, amount=D("40"), currency="USDT", account=account()
        )
        session.add(
            Order(
                environment="paper",
                intent_id=identifier,
                order_link_id="frozen",
                category="spot",
                symbol="BTCUSDT",
                side="buy",
                order_type="Limit",
                time_in_force="GTC",
                price=D("100"),
                qty=D(".4"),
                qty_filled=D("0"),
                status="ACCEPTED",
                sync_state="CONFIRMED",
                reservation_id=reservation.id,
            )
        )
        await session.commit()
    async with sessionmaker() as session:
        missing = await manager.budget_view(session, account(available="60", locked="40"), "USDT")
        proven = await manager.budget_view(
            session, account(available="60", locked="40", reflected=["frozen"]), "USDT"
        )
        assert missing.usable == D("20")
        assert proven.usable == D("60")
        unverified = AccountView(
            "paper-account", "unified", {"USDT": CoinBalance("USDT", D("999"), D("999"))}
        )
        with pytest.raises(ReservationError, match="缺少自有现金"):
            await manager.budget_view(session, unverified, "USDT")


async def test_inventory_close_competition_and_strategy_ownership(sessionmaker):
    strategy_id, identifier = await seed(sessionmaker)
    async with sessionmaker() as session:
        session.add(
            Inventory(
                environment="paper",
                strategy_id=strategy_id,
                symbol="BTCUSDT",
                qty=D("1"),
                avg_cost=D("10"),
                currency="USDT",
                trusted=True,
                cost_source="bot_fill",
            )
        )
        await session.commit()
    manager = ReservationManager("paper")

    async def reserve():
        async with sessionmaker() as session:
            try:
                row = await manager.reserve_inventory(
                    session,
                    intent_id=identifier,
                    symbol="BTCUSDT",
                    qty=D(".7"),
                    account_qty=D("4"),
                    account_trusted=True,
                    strategy_id=strategy_id,
                )
                await session.commit()
                return row.id
            except ReservationError:
                return None

    results = await asyncio.gather(reserve(), reserve())
    assert sum(item is not None for item in results) == 1
    async with sessionmaker() as session:
        assert await manager.closeable_qty(
            session,
            symbol="BTCUSDT",
            strategy_id=strategy_id,
            account_qty=D("4"),
            account_trusted=True,
        ) == D(".3")


async def test_inventory_freeze_overlap_requires_snapshot_evidence(sessionmaker):
    strategy_id, identifier = await seed(sessionmaker)
    manager = ReservationManager("paper")
    async with sessionmaker() as session:
        session.add(Inventory(environment="paper", strategy_id=strategy_id, symbol="BTCUSDT",
                              qty=D("1"), avg_cost=D("10"), currency="USDT", trusted=True))
        await session.flush()
        held = await manager.reserve_inventory(session, intent_id=identifier, symbol="BTCUSDT",
            qty=D(".4"), account_qty=D("1"), account_trusted=True, strategy_id=strategy_id)
        session.add(Order(environment="paper", intent_id=identifier, reservation_id=held.id,
            order_link_id="sell-frozen", category="spot", symbol="BTCUSDT", side="sell",
            order_type="Limit", time_in_force="GTC", price=D("10"), qty=D(".4"),
            qty_filled=D("0"), status="ACCEPTED", sync_state="CONFIRMED"))
        await session.commit()
    async with sessionmaker() as session:
        kwargs = {"symbol": "BTCUSDT", "strategy_id": strategy_id, "account_qty": D("1"),
                  "account_available": D(".6"), "account_trusted": True}
        assert await manager.closeable_qty(session, **kwargs) == D(".2")
        assert await manager.closeable_qty(session, **kwargs,
            reflected_order_link_ids=["sell-frozen"]) == D(".6")
        assert await manager.closeable_qty(session, **kwargs,
            reflected_order_link_ids=["unrelated"]) == D(".2")


async def test_costs_follow_fee_units_and_strategy(sessionmaker):
    strategy_id, _ = await seed(sessionmaker)
    ledger = LedgerWriter("paper")
    async with sessionmaker() as session:
        change, realised = await ledger.apply_fill_to_inventory(
            session,
            strategy_id=strategy_id,
            symbol="BTCUSDT",
            side="buy",
            qty=D("2"),
            price=D("10"),
            fee=D(".1"),
            fee_asset="BTC",
            currency="USDT",
            base_asset="BTC",
        )
        assert change == D("1.9") and realised == 0
        inventory = await session.scalar(select(Inventory))
        assert abs(inventory.avg_cost * inventory.qty - D("20")) < D("1e-15")
        _, realised = await ledger.apply_fill_to_inventory(
            session,
            strategy_id=strategy_id,
            symbol="BTCUSDT",
            side="sell",
            qty=D(".9"),
            price=D("20"),
            fee=D(".2"),
            fee_asset="USDT",
            currency="USDT",
            base_asset="BTC",
        )
        assert abs(realised - (D("18") - D("20") / D("1.9") * D(".9") - D(".2"))) < D("1e-15")
        assert inventory.qty == D("1")
        await session.commit()


async def test_schedule_100_duplicates_claim_once_and_skip_missed(sessionmaker):
    strategy_id, _ = await seed(sessionmaker)
    scheduler = Scheduler(sessionmaker, "paper", skip_grace_seconds=60)
    moment = now_utc() - dt.timedelta(seconds=1)
    planned = await asyncio.gather(
        *(
            scheduler.plan(
                strategy_id=strategy_id, strategy_version=1, rule_id="daily", planned_at=moment
            )
            for _ in range(100)
        )
    )
    assert sum(item is not None for item in planned) == 1
    claims = await asyncio.gather(*(scheduler.claim_due() for _ in range(5)))
    assert sum(len(item) for item in claims) == 1
    run = next(item[0] for item in claims if item)
    await scheduler.finish(run.id, status="DONE")
    await scheduler.finish(run.id, status="FAILED")
    assert await scheduler.in_cooldown(
        strategy_id=strategy_id, rule_id="daily", cooldown_seconds=60
    )
    await scheduler.plan(
        strategy_id=strategy_id,
        strategy_version=1,
        rule_id="old",
        planned_at=moment - dt.timedelta(hours=1),
    )
    assert not await scheduler.claim_due()
    async with sessionmaker() as session:
        assert await session.scalar(select(func.count()).select_from(ScheduleRun)) == 2
        assert (
            await session.scalar(select(ScheduleRun.status).where(ScheduleRun.id == run.id))
            == "DONE"
        )
        assert (
            await session.scalar(select(ScheduleRun.status).where(ScheduleRun.rule_id == "old"))
            == "SKIPPED"
        )


def test_global_physical_ids():
    ids = {order_link_id_for(1, 0) for _ in range(1000)}
    assert len(ids) == 1000
    assert all(len(item) <= 36 for item in ids)
