"""Synthetic non-paper settlement reconciliation; all network is blocked."""

from __future__ import annotations

import datetime as dt
from dataclasses import replace
from decimal import Decimal as D

import pytest
from sqlalchemy import func, select

from bybitbot.adapters.base import AccountView, CoinBalance, OptionDeliveryView, PositionView
from bybitbot.clock import now_utc
from bybitbot.db.models import (
    AccountSnapshot,
    CashFlow,
    Instrument,
    Inventory,
    LedgerEvent,
    Strategy,
)
from bybitbot.reconciliation import Reconciler


class DeliveryAccount:
    environment = "testnet"
    allow_trade_writes = False

    def __init__(self, fact, balance):
        self.fact = fact
        self.balance = balance
        self.positions = []
        self.ready = True

    async def get_option_deliveries(self, spec, *, start_time, end_time):
        assert spec.symbol == self.fact.symbol
        assert start_time <= self.fact.occurred_at <= end_time
        return [self.fact] if self.ready else []

    async def get_account(self):
        return AccountView(
            "synthetic-uid", "unified",
            {"USDT": CoinBalance("USDT", self.balance, self.balance, self.balance)},
        )

    async def get_positions(self, category):
        return self.positions


async def seeded_account(sessionmaker, *, payout, fee=D("0"), drift=D("0")):
    expiry = now_utc() - dt.timedelta(minutes=2)
    fact = OptionDeliveryView(
        settlement_id="bybit:synthetic-delivery", symbol="BTC-1NOV26-100-C-USDT",
        side="Buy", qty=D("1"), currency="USDT", payout=payout, fee=fee,
        occurred_at=expiry, delivery_price=D("100") + payout, strike_price=D("100"),
        reference="synthetic-bybit-delivery", evidence={"transaction_log_ids": ["synthetic-log"]},
    )
    async with sessionmaker() as session:
        strategy = Strategy(
            environment="testnet", name="delivery-test", kind="option_single_leg_buy"
        )
        session.add(strategy)
        await session.flush()
        session.add_all([
            Instrument(
                environment="testnet", category="option", symbol=fact.symbol,
                base_coin="BTC", quote_coin="USDT", settle_coin="USDT", status="Settled",
                strike_price=D("100"), options_type="Call", delivery_time=expiry,
            ),
            Inventory(
                environment="testnet", strategy_id=strategy.id, symbol=fact.symbol,
                qty=D("1"), avg_cost=D("10"), currency="USDT", trusted=True,
                cost_source="bot_fill",
            ),
            AccountSnapshot(
                environment="testnet", account_uid="synthetic-uid", account_mode="unified",
                coins={"USDT": {"wallet_balance": "1000"}}, complete=True,
                captured_at=expiry - dt.timedelta(minutes=1),
            ),
        ])
        await session.commit()
    adapter = DeliveryAccount(fact, D("1000") + payout - fee + drift)
    reconciler = Reconciler(
        adapter=adapter, sessionmaker=sessionmaker, environment="testnet",
        account_uid="synthetic-uid",
    )
    return adapter, reconciler


@pytest.mark.parametrize("payout,fee", [(D("0"), D("0")), (D("20"), D("0.1"))])
async def test_exchange_delivery_reconciles_cash_and_releases_cost_once(sessionmaker, payout, fee):
    adapter, reconciler = await seeded_account(sessionmaker, payout=payout, fee=fee)
    first = await reconciler.reconcile_settlements()
    assert first.allows_resume and first.repaired, first.summary()
    assert (await reconciler.reconcile_cash()).allows_resume
    assert (await reconciler.reconcile_positions()).allows_resume
    replay = await reconciler.reconcile_settlements()
    assert replay.allows_resume and not replay.repaired
    async with sessionmaker() as session:
        inventory = await session.scalar(select(Inventory))
        assert inventory.qty == 0 and inventory.avg_cost == 0
        assert await session.scalar(select(func.count()).select_from(LedgerEvent)) == 1
        event = await session.scalar(select(LedgerEvent))
        assert D(event.payload["allocations"][0]["realised_pnl"]) == payout - fee - D("10")
        assert event.payload["evidence"]["cash_anchor_snapshot_id"]
        assert (await session.scalar(select(CashFlow))).amount == payout - fee


@pytest.mark.parametrize("failure", ["missing", "quantity", "position", "cash", "unknown_cost"])
async def test_unverified_delivery_keeps_cost_until_evidence_recovers(sessionmaker, failure):
    adapter, reconciler = await seeded_account(sessionmaker, payout=D("20"))
    original = adapter.fact
    if failure == "missing":
        adapter.ready = False
    elif failure == "quantity":
        adapter.fact = replace(adapter.fact, qty=D("2"))
    elif failure == "position":
        adapter.positions = [PositionView("option", adapter.fact.symbol, size=D("1"), side="Buy")]
    elif failure == "cash":
        adapter.balance = D("1000")
    else:
        async with sessionmaker() as session:
            (await session.scalar(select(Inventory))).cost_source = "unknown"
            await session.commit()
    result = await reconciler.reconcile_settlements()
    assert not result.allows_resume and not result.repaired
    assert any(d.kind == "SETTLEMENT_UNVERIFIED" for d in result.differences)
    async with sessionmaker() as session:
        inventory = await session.scalar(select(Inventory))
        assert inventory.qty == 1 and inventory.avg_cost == 10
        assert await session.scalar(select(func.count()).select_from(LedgerEvent)) == 0
    if failure == "unknown_cost":
        return
    # A failed position comparison and subsequent incomplete snapshot must not make
    # the delivery irrecoverable, nor absorb its delayed cash change into a baseline.
    await reconciler.reconcile_positions()
    async with sessionmaker() as session:
        session.add(AccountSnapshot(
            environment="testnet", account_uid="synthetic-uid", account_mode="unified",
            coins={"USDT": {"wallet_balance": str(adapter.balance)}}, complete=False,
            captured_at=now_utc(),
        ))
        await session.commit()
    adapter.ready, adapter.fact, adapter.positions, adapter.balance = True, original, [], D("1020")
    recovered = await reconciler.reconcile_settlements()
    assert recovered.allows_resume and recovered.repaired, recovered.summary()
    assert (await reconciler.reconcile_cash()).allows_resume
    assert (await reconciler.reconcile_positions()).allows_resume


async def test_external_cash_drift_prevents_delivery_release(sessionmaker):
    _, reconciler = await seeded_account(sessionmaker, payout=D("20"), drift=D("5"))
    result = await reconciler.reconcile_settlements()
    assert not result.allows_resume
    async with sessionmaker() as session:
        assert (await session.scalar(select(Inventory))).qty == 1
        assert await session.scalar(select(func.count()).select_from(CashFlow)) == 0


async def test_incomplete_snapshots_cannot_hide_external_cash_changes(sessionmaker):
    adapter, reconciler = await seeded_account(sessionmaker, payout=D("20"))
    adapter.balance = D("1025")
    for _ in range(2):
        async with sessionmaker() as session:
            session.add(AccountSnapshot(
                environment="testnet", account_uid="synthetic-uid", account_mode="unified",
                coins={"USDT": {"wallet_balance": "1025"}}, complete=False,
            ))
            await session.commit()
        assert not (await reconciler.reconcile_cash()).allows_resume
        assert not (await reconciler.reconcile_settlements()).allows_resume
