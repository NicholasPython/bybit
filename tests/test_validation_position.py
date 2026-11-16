"""逐腿成交拒绝原子性、退出顺序与未解决敞口回归。"""

from copy import deepcopy
from decimal import Decimal as D

import pytest

from bybitbot.validation import (
    ExitQuote,
    IntentPurpose,
    PendingIntent,
    PositionLedger,
    SpreadProtocol,
    StudyPhase,
    detect_gap,
)
from bybitbot.validation.protocol import DAY_MS, HOUR_MS


def opening():
    ledger = PositionLedger(base="BTC", quantity=D("0.01"), reserved_risk=D("10"))
    ledger.set_pending(
        PendingIntent(
            IntentPurpose.OPEN_LONG,
            "long",
            "Buy",
            1000,
            990,
            deadline_ms=2000,
            limit=D("100"),
        )
    )
    return ledger


def apply(ledger, **changes):
    values = {
        "purpose": IntentPurpose.OPEN_LONG,
        "symbol": "long",
        "side": "Buy",
        "price": D("99"),
        "qty": D("0.01"),
        "fee": D("0.01"),
        "quote_source_ms": 1100,
        "now_ms": 1100,
    }
    values.update(changes)
    return ledger.apply_fill(**values)


@pytest.mark.parametrize(
    "changes",
    [
        {"quote_source_ms": 990},
        {"qty": D("0.02")},
        {"fee": D("-0.01")},
        {"price": D("NaN")},
        {"qty": D("Infinity")},
        {"price": D("101")},
        {"now_ms": 2000},
        {"side": "Sell"},
        {"symbol": "other"},
        {"quote_source_ms": 1200, "now_ms": 1100},
        {"quote_source_ms": 1100.5},
        {"now_ms": True},
    ],
)
def test_rejected_fill_keeps_entire_ledger_unchanged(changes):
    ledger = opening()
    before = deepcopy(ledger)
    with pytest.raises(ValueError):
        apply(ledger, **changes)
    assert ledger == before


def test_invalid_phase_does_not_apply_half_a_fill():
    ledger = opening()
    ledger.phase = StudyPhase.WAITING
    before = deepcopy(ledger)
    with pytest.raises(ValueError, match="阶段"):
        apply(ledger)
    assert ledger == before


def test_pending_cannot_be_overwritten_or_reopened_after_cancel():
    ledger = opening()
    before = deepcopy(ledger)
    with pytest.raises(ValueError):
        ledger.set_pending(PendingIntent(IntentPurpose.OPEN_LONG, "long", "Buy", 1100, 1000))
    assert ledger == before
    assert ledger.abort_entry("timeout") is StudyPhase.CANCELLED
    assert ledger.reserved_risk == 0
    with pytest.raises(ValueError):
        ledger.set_pending(PendingIntent(IntentPurpose.OPEN_LONG, "long", "Buy", 1100, 1000))


def test_abort_after_first_leg_preserves_risk_until_flat():
    ledger = opening()
    apply(ledger)
    assert ledger.abort_entry("signal_lost") is StudyPhase.ABORTING
    assert ledger.pending is None
    assert ledger.reserved_risk == D("10")
    ledger.set_pending(PendingIntent(IntentPurpose.CLOSE_LONG, "long", "Sell", 1200, 1190))
    ledger.apply_fill(
        purpose=IntentPurpose.CLOSE_LONG,
        symbol="long",
        side="Sell",
        price=D("98"),
        qty=D("0.01"),
        fee=D("0.01"),
        quote_source_ms=1250,
        now_ms=1250,
    )
    assert ledger.phase is StudyPhase.ABORTED
    assert ledger.cash == D("-0.03")
    assert ledger.fees == D("0.02")
    assert ledger.reserved_risk == 0


@pytest.mark.parametrize("ending", ["end_at_expiry", "end_at_study_end"])
def test_unresolved_retains_risk_and_cannot_claim_closed(ending):
    ledger = opening()
    apply(ledger)
    cash, fees = ledger.cash, ledger.fees
    target = getattr(ledger, ending)()
    assert target in {StudyPhase.EXPIRED_UNRESOLVED, StudyPhase.STUDY_ENDED_UNRESOLVED}
    assert ledger.long_held and ledger.pending is None
    assert ledger.reserved_risk == D("10")
    assert (ledger.cash, ledger.fees) == (cash, fees)
    assert ledger.net_value({"long": ExitQuote(D("100"))}, SpreadProtocol()) is None
    assert getattr(ledger, ending)() is target
    with pytest.raises(ValueError):
        ledger.transition(StudyPhase.CLOSED)


def test_open_short_requires_long_and_close_long_requires_short_flat():
    ledger = PositionLedger(base="BTC", quantity=D("0.01"), phase=StudyPhase.OPENING_SHORT)
    with pytest.raises(ValueError):
        ledger.set_pending(PendingIntent(IntentPurpose.OPEN_SHORT, "short", "Sell", 1000, 990))
    ledger.phase = StudyPhase.CLOSING_LONG
    ledger.long_held = ledger.short_held = True
    with pytest.raises(ValueError):
        ledger.set_pending(PendingIntent(IntentPurpose.CLOSE_LONG, "long", "Sell", 1000, 990))
    with pytest.raises(ValueError):
        ledger.transition(StudyPhase.CANCELLED)


def test_valuation_requires_identity_for_each_held_leg():
    ledger = PositionLedger(
        base="BTC",
        phase=StudyPhase.OPENING_SHORT,
        quantity=D("0.01"),
        long_held=True,
    )
    assert ledger.net_value({}, SpreadProtocol()) is None
    ledger.long_symbol = "long"
    assert ledger.net_value({"long": ExitQuote(D("NaN"))}, SpreadProtocol()) is None


def test_exit_priority_and_exact_thresholds():
    now = 100 * DAY_MS
    ledger = PositionLedger(
        base="BTC",
        phase=StudyPhase.OPEN,
        quantity=D("0.01"),
        long_held=True,
        short_held=True,
        opened_ms=now - HOUR_MS,
        entry_debit=D("10"),
    )
    args = {
        "now_ms": now,
        "end_ms": now + DAY_MS,
        "expiry_ms": now + 10 * DAY_MS,
        "signal_eligible": True,
        "net_pnl": None,
        "protocol": SpreadProtocol(),
    }
    assert ledger.exit_trigger(**args) is None
    assert ledger.exit_trigger(**(args | {"net_pnl": D("5")})) == "take_profit"
    assert ledger.exit_trigger(**(args | {"net_pnl": D("-5")})) == "stop_loss"
    assert ledger.exit_trigger(**(args | {"net_pnl": D("4.999")})) is None
    assert ledger.exit_trigger(**(args | {"signal_eligible": False})) == "signal_lost"
    assert (
        ledger.exit_trigger(
            **(
                args
                | {
                    "expiry_ms": now + DAY_MS,
                    "signal_eligible": False,
                }
            )
        )
        == "before_expiry"
    )
    ledger.opened_ms = now - 72 * HOUR_MS
    assert ledger.exit_trigger(**(args | {"expiry_ms": now})) == "max_hold"
    assert ledger.exit_trigger(**(args | {"end_ms": now + HOUR_MS})) == "study_end_exit"


@pytest.mark.parametrize(
    "changes",
    [
        {"side": "Sell"},
        {"created_ms": -1},
        {"decision_source_ms": 1001},
        {"deadline_ms": 999},
        {"limit": D("NaN")},
        {"purpose": "unknown"},
    ],
)
def test_invalid_pending_intent_rejected(changes):
    args = {
        "purpose": IntentPurpose.OPEN_LONG,
        "symbol": "long",
        "side": "Buy",
        "created_ms": 1000,
        "decision_source_ms": 990,
    }
    with pytest.raises(ValueError):
        PendingIntent(**(args | changes))


@pytest.mark.parametrize("maximum", [0, -1, True])
def test_gap_requires_positive_interval(maximum):
    with pytest.raises(ValueError):
        detect_gap(base="BTC", previous_ms=1000, observed_ms=2000, max_gap_seconds=maximum)
