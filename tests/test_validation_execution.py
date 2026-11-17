"""Persistent orchestration follows actual fills, not acknowledgements."""

import datetime as dt
from dataclasses import asdict
from decimal import Decimal as D

import pytest
from sqlalchemy import select

from bybitbot.db.models import Execution, Order, Strategy, StrategyConfigVersion, TradeIntent
from bybitbot.strategy.base import SignalContext, build_intent
from bybitbot.validation import DAY_MS, SpreadLeg, SpreadProtocol
from bybitbot.validation.models import ValidationLedger, ValidationPosition
from bybitbot.validation.orchestration import ValidationOrchestrator
from bybitbot.validation.sampling import ValidationStore

NOW = 1_800_000_000_000


def legs(moment=NOW, direction="Call"):
    shared = {
        "base": "BTC",
        "status": "Trading",
        "option_type": direction,
        "expiry_ms": NOW + 21 * DAY_MS,
        "index_price": D(30000),
        "bid_size": D(10),
        "ask_size": D(10),
        "tick_size": D("0.5"),
        "qty_step": D("0.01"),
        "min_qty": D("0.01"),
        "max_qty": D(10),
        "min_price": D("0.5"),
        "max_price": D(100000),
        "source_ms": moment,
    }
    return [
        SpreadLeg(
            symbol="BTC-long",
            strike=D(30000 if direction == "Call" else 31000),
            bid=D(1490),
            ask=D(1500),
            delta=D("0.55" if direction == "Call" else "-0.55"),
            **shared,
        ),
        SpreadLeg(
            symbol="BTC-short",
            strike=D(31000 if direction == "Call" else 30000),
            bid=D(890),
            ask=D(900),
            delta=D("0.35" if direction == "Call" else "-0.35"),
            **shared,
        ),
    ]


async def setup(sessionmaker, direction="Call"):
    store = ValidationStore(sessionmaker, "paper", "account")
    study = await store.start(
        run_id="spread",
        bases=["BTC"],
        policy="strong_up" if direction == "Call" else "weak_down",
        direction=direction,
        protocol=SpreadProtocol(),
        started_ms=NOW,
    )
    async with sessionmaker() as session:
        strategy = Strategy(
            environment="paper", name="validation", kind="validation_spread", enabled=True
        )
        session.add(strategy)
        await session.flush()
        session.add(
            StrategyConfigVersion(
                strategy_id=strategy.id,
                version=1,
                params={"kind": "validation_spread"},
                exit_rules=[],
                symbols=[],
                schedule_utc=[],
                budget_per_run=D(100),
                max_spend_per_trade=D(100),
                currency="USDT",
                active=True,
            )
        )
        await session.commit()
        strategy_id = strategy.id
    await store.bind(study.id, strategy_id=strategy_id, version=1)
    await store.record(
        study.id,
        slot_ms=NOW,
        observed_ms=NOW,
        payload={
            "bases": {
                "BTC": {
                    "signal": {"eligible": True},
                    "legs": [asdict(leg) for leg in legs(direction=direction)],
                }
            }
        },
    )
    return store, study


class CanonicalFacts:
    """Controllable boundary: orchestration tests never claim to test matching/risk."""

    def __init__(self, sessionmaker):
        self.sessionmaker = sessionmaker
        self.submissions = []
        self.cancelled = []

    async def submit(self, position_id, binding, pending, draft):
        self.submissions.append(pending)
        async with self.sessionmaker() as session:
            if await session.scalar(
                select(TradeIntent).where(TradeIntent.signal_id == pending["signal_id"])
            ):
                return
            intent = build_intent(
                SignalContext(
                    environment="paper",
                    account_uid="account",
                    strategy_id=binding["strategy_id"],
                    strategy_version=binding["version"],
                    rule_id="unused",
                    planned_at=dt.datetime.fromtimestamp(NOW / 1000, dt.UTC),
                ),
                draft,
            )
            intent.signal_id = pending["signal_id"]
            intent.status = "SUBMITTED"
            session.add(intent)
            await session.flush()
            session.add(
                Order(
                    environment="paper",
                    intent_id=intent.id,
                    order_link_id=pending["signal_id"],
                    category="option",
                    symbol=draft.symbol,
                    side=draft.side,
                    order_type="Limit",
                    time_in_force="GTC",
                    price=D(pending["price"]),
                    qty=draft.qty,
                    status="NEW",
                    qty_filled=D(0),
                    sync_state="CONFIRMED",
                    reduce_only=pending["purpose"].startswith("close_"),
                )
            )
            await session.commit()

    async def cancel(self, order):
        # Ack alone does not change the terminal fact.
        self.cancelled.append(order.id)

    async def fill(self, pending, qty=None, *, terminal=True, suffix="1", at=NOW + 1):
        async with self.sessionmaker() as session:
            order = await session.scalar(
                select(Order).where(Order.order_link_id == pending["signal_id"])
            )
            qty = order.qty if qty is None else qty
            order.qty_filled += qty
            order.status = "FILLED" if terminal else "PARTIALLY_FILLED"
            session.add(
                Execution(
                    environment="paper",
                    category="option",
                    exchange_exec_id=f"{order.id}:{suffix}",
                    order_id=order.id,
                    order_link_id=order.order_link_id,
                    symbol=order.symbol,
                    side=order.side,
                    exec_price=order.price,
                    exec_qty=qty,
                    exec_fee=D("0.09"),
                    exec_fee_asset="USDT",
                    exec_time=dt.datetime.fromtimestamp(at / 1000, dt.UTC),
                    raw={"quote_source_ms": at},
                )
            )
            await session.commit()

    async def terminal(self, pending, status="CANCELED"):
        async with self.sessionmaker() as session:
            order = await session.scalar(
                select(Order).where(Order.order_link_id == pending["signal_id"])
            )
            order.status = status
            await session.commit()


async def position(store):
    async with store.sessionmaker() as session:
        return await session.scalar(select(ValidationPosition))


@pytest.mark.parametrize("direction", ["Call", "Put"])
async def test_four_legs_project_cash_fees_latency_and_restart(sessionmaker, direction):
    store, _ = await setup(sessionmaker, direction)
    facts = CanonicalFacts(sessionmaker)
    engine = ValidationOrchestrator(store, submit=facts.submit, cancel=facts.cancel)
    await engine.tick(now_ms=NOW, allows_open=True)
    assert len(facts.submissions) == 1
    opening = facts.submissions[-1]
    await engine.tick(now_ms=NOW + 1, allows_open=True)
    assert len(facts.submissions) == 1  # Accepted is not filled.
    await facts.fill(opening, at=NOW + 2)
    await engine.tick(now_ms=NOW + 3, allows_open=True)
    assert facts.submissions[-1]["purpose"] == "open_short"
    await facts.fill(facts.submissions[-1], at=NOW + 4)
    engine = ValidationOrchestrator(
        ValidationStore(sessionmaker, "paper", "account"), submit=facts.submit, cancel=facts.cancel
    )
    await engine.tick(now_ms=NOW + 5, allows_open=True)
    row = await position(store)
    assert row.phase == "open" and row.state["long_qty"] == row.state["short_qty"]
    async with sessionmaker() as session:
        row = await session.get(ValidationPosition, row.id)
        row.state = row.state | {"stop_requested": True}
        await session.commit()
    await engine.tick(now_ms=NOW + 6, allows_open=False)
    assert facts.submissions[-1]["purpose"] == "close_short"
    await facts.fill(facts.submissions[-1], at=NOW + 7)
    await engine.tick(now_ms=NOW + 8, allows_open=False)
    assert facts.submissions[-1]["purpose"] == "close_long"
    await facts.fill(facts.submissions[-1], at=NOW + 9)
    await engine.tick(now_ms=NOW + 10, allows_open=False)
    row = await position(store)
    assert row.phase == "closed" and row.reserved_risk == 0
    assert row.fees == D("0.36")
    assert row.cash == -D("22") * D(opening["qty"]) - D("0.36")
    async with sessionmaker() as session:
        events = list(
            await session.scalars(
                select(ValidationLedger).where(ValidationLedger.kind == "execution")
            )
        )
        assert len(events) == 4
        assert events[0].payload["latency_seconds"] == "0.002"
    await engine.tick(now_ms=NOW + 11, allows_open=True)
    assert len(facts.submissions) == 4


async def test_partial_long_cancel_ack_does_not_release_or_start_short(sessionmaker):
    store, _ = await setup(sessionmaker)
    facts = CanonicalFacts(sessionmaker)
    engine = ValidationOrchestrator(store, submit=facts.submit, cancel=facts.cancel)
    await engine.tick(now_ms=NOW, allows_open=True)
    opening = facts.submissions[-1]
    await facts.fill(opening, D("0.01"), terminal=False, at=NOW + 1)
    await engine.tick(now_ms=NOW + 2, allows_open=False)
    row = await position(store)
    assert row.phase == "opening_long" and row.reserved_risk > 0
    assert facts.cancelled and len(facts.submissions) == 1
    # Late second fill before terminal confirmation is included in the unwind.
    await facts.fill(opening, D("0.01"), terminal=False, suffix="late", at=NOW + 3)
    await facts.terminal(opening)
    await engine.tick(now_ms=NOW + 4, allows_open=False)
    close = facts.submissions[-1]
    assert close["purpose"] == "close_long" and D(close["qty"]) == D("0.02")
    await facts.fill(close, at=NOW + 5)
    await engine.tick(now_ms=NOW + 6, allows_open=False)
    row = await position(store)
    assert row.phase == "aborted" and row.reserved_risk == 0 and row.fees == D("0.27")


async def test_terminal_order_waits_for_actual_execution_before_advancing(sessionmaker):
    store, _ = await setup(sessionmaker)
    facts = CanonicalFacts(sessionmaker)
    engine = ValidationOrchestrator(store, submit=facts.submit, cancel=facts.cancel)
    await engine.tick(now_ms=NOW, allows_open=True)
    async with sessionmaker() as session:
        order = await session.scalar(select(Order))
        order.status, order.qty_filled = "FILLED", order.qty
        await session.commit()
    await engine.tick(now_ms=NOW + 1, allows_open=True)
    assert len(facts.submissions) == 1
    assert (await position(store)).phase == "opening_long"


async def test_strategy_binding_cannot_be_replaced_after_start(sessionmaker):
    store, study = await setup(sessionmaker)
    row = await position(store)
    with pytest.raises(ValueError):
        await store.bind(study.id, strategy_id=row.state["binding"]["strategy_id"], version=2)
    assert (await position(store)).state == row.state
