"""验证层纯函数测试（缺口 1/2/3/5/9 的口径回归）。

这些用例把研究侧冻结协议的判断口径钉住：改一个阈值或一个取整方向，
这里就会红。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from bybitbot.marketdata.snapshots import completed_closes
from bybitbot.validation import (
    ExitQuote,
    IntentPurpose,
    PendingIntent,
    PositionLedger,
    SignalPolicy,
    SpreadLeg,
    SpreadProtocol,
    StudyPhase,
    bear_contract_ok,
    choose_candidate,
    detect_gap,
    evaluate_signal,
    executable_price,
    limit_price,
    remaining_risk_budget,
    sma,
    spread_economics,
    structure_ok,
)

D = Decimal
HOUR = 3_600_000
DAY = 24 * HOUR
NOW = 1_760_000_000_000


def protocol() -> SpreadProtocol:
    return SpreadProtocol()


def long_leg(**overrides) -> SpreadLeg:
    """多头腿：30000 行权价的 Call，权利金约 1500。"""
    data = {
        "symbol": "BTC-21DEC26-30000-C-USDT",
        "base": "BTC",
        "status": "Trading",
        "option_type": "Call",
        "expiry_ms": NOW + 21 * DAY,
        "strike": D("30000"),
        "index_price": D("30000"),
        "bid": D("1490"),
        "ask": D("1500"),
        "bid_size": D("10"),
        "ask_size": D("10"),
        "tick_size": D("0.5"),
        "qty_step": D("0.01"),
        "min_qty": D("0.01"),
        "max_qty": D("1"),
        "min_price": D("0.01"),
        "max_price": D("100000"),
        "delta": D("0.55"),
        "source_ms": NOW,
    }
    data.update(overrides)
    return SpreadLeg(**data)


def short_leg(**overrides) -> SpreadLeg:
    """空头腿：31000 行权价的 Call，权利金约 900。"""
    data = {
        "symbol": "BTC-21DEC26-31000-C-USDT",
        "base": "BTC",
        "status": "Trading",
        "option_type": "Call",
        "expiry_ms": NOW + 21 * DAY,
        "strike": D("31000"),
        "index_price": D("30000"),
        "bid": D("890"),
        "ask": D("900"),
        "bid_size": D("10"),
        "ask_size": D("10"),
        "tick_size": D("0.5"),
        "qty_step": D("0.01"),
        "min_qty": D("0.01"),
        "max_qty": D("1"),
        "min_price": D("0.01"),
        "max_price": D("100000"),
        "delta": D("0.35"),
        "source_ms": NOW,
    }
    data.update(overrides)
    return SpreadLeg(**data)


# --------------------------------------------------------------------------
# 指标与信号
# --------------------------------------------------------------------------


def test_sma_uses_last_window():
    assert sma([D("1"), D("2"), D("3"), D("4")], 2) == D("3.5")
    with pytest.raises(ValueError):
        sma([D("1")], 2)


def test_strong_up_signal_requires_trend_and_return():
    days = [(index * DAY, D(100 + index)) for index in range(51)]
    hours = [(index * HOUR, D("100")) for index in range(22)]
    hours += [(index * HOUR, D("200")) for index in range(22, 25)]
    signal = evaluate_signal(hourly=hours, daily=days, policy=SignalPolicy.STRONG_UP)
    assert signal.eligible is True
    assert signal.reason == "strong_up"
    assert signal.return_24h == D("1")
    assert signal.previous_sma20 is not None
    assert signal.last3_hourly_closes == (D("200"), D("200"), D("200"))


def test_weak_down_signal_requires_hour_below_both_sma():
    days = [(index * DAY, D(150 - index)) for index in range(51)]
    hours = [(index * HOUR, D("100")) for index in range(24)]
    hours.append((24 * HOUR, D("50")))
    signal = evaluate_signal(hourly=hours, daily=days, policy=SignalPolicy.WEAK_DOWN)
    assert signal.eligible is True
    assert signal.reason == "weak_down"


def test_signal_reports_insufficient_history_instead_of_raising():
    signal = evaluate_signal(
        hourly=[(0, D("1"))], daily=[(0, D("1"))], policy=SignalPolicy.STRONG_UP
    )
    assert signal.eligible is False
    assert signal.reason == "insufficient_hourly_history"


def test_completed_closes_rejects_incomplete_or_broken_history():
    cutoff = 25 * HOUR
    good = [[index * HOUR, "1", "2", "0.5", "1.5", "10", "20"] for index in range(25)]
    rows = good + [[25 * HOUR, "1", "2", "0.5", "1.5", "10", "20"]]
    closes = completed_closes(rows, cutoff, HOUR, 25)
    assert len(closes) == 25
    assert closes[-1][1] == D("1.5")

    # 未完成的 K 线不计入，但要求最后一根正好贴住截止时刻。
    with pytest.raises(ValueError):
        completed_closes(good, cutoff, HOUR, 26)
    broken = good[:-1]
    with pytest.raises(ValueError):
        completed_closes(broken, cutoff, HOUR, 25)


# --------------------------------------------------------------------------
# 两腿结构与可执行价格
# --------------------------------------------------------------------------


def test_structure_requires_lower_strike_long_leg_and_entry_window():
    assert structure_ok(long_leg(), short_leg(), NOW, protocol()) is True
    assert structure_ok(short_leg(), long_leg(), NOW, protocol()) is False
    assert structure_ok(long_leg(), short_leg(strike=D("29000")), NOW, protocol()) is False
    outside = long_leg(expiry_ms=NOW + 5 * DAY)
    assert structure_ok(outside, short_leg(expiry_ms=NOW + 5 * DAY), NOW, protocol()) is False


def test_structure_enforces_delta_bands():
    assert structure_ok(long_leg(delta=D("0.70")), short_leg(), NOW, protocol()) is False
    assert structure_ok(long_leg(delta=D("0.20")), short_leg(), NOW, protocol()) is False
    # 成交复核时按研究口径不查 Delta。
    assert (
        structure_ok(long_leg(delta=D("0.70")), short_leg(), NOW, protocol(), check_deltas=False)
        is True
    )


def test_structure_enforces_width_index_band():
    wide = long_leg(strike=D("28000"))
    assert structure_ok(wide, short_leg(), NOW, protocol()) is False


def test_executable_price_applies_adverse_slippage():
    leg = long_leg()
    assert executable_price(leg, "Buy", D("0.01"), protocol()) == D("1500.5")
    assert executable_price(leg, "Sell", D("0.01"), protocol()) == D("1489.5")
    assert executable_price(leg, "Buy", D("0.01"), protocol(), ticks=2) == D("1501")


def test_executable_price_rejects_wide_spread_on_entry_only():
    leg = long_leg(bid=D("1400"), ask=D("1600"))
    assert executable_price(leg, "Buy", D("0.01"), protocol(), entry=True) is None
    assert executable_price(leg, "Buy", D("0.01"), protocol()) is not None


def test_executable_price_rejects_bad_quantity_and_size():
    leg = long_leg()
    assert executable_price(leg, "Buy", D("0.015"), protocol()) is None
    assert executable_price(leg, "Buy", D("20"), protocol()) is None
    small = long_leg(ask_size=D("0.005"))
    assert executable_price(small, "Buy", D("0.01"), protocol()) is None
    untradable = long_leg(status="Closed")
    assert executable_price(untradable, "Buy", D("0.01"), protocol()) is None


def test_limit_price_rounds_in_the_safe_direction():
    leg = long_leg()
    assert limit_price(leg, "Buy", protocol()) == D("1530")
    assert limit_price(short_leg(), "Sell", protocol()) == D("872")


# --------------------------------------------------------------------------
# 价差级成本闸门
# --------------------------------------------------------------------------


def test_spread_economics_passes_all_gates_for_a_sane_spread():
    economics = spread_economics(long_leg(), short_leg(), D("0.01"), protocol())
    assert economics is not None
    assert economics.eligible is True
    assert economics.failed_gates == ()
    assert economics.debit == D("6.29")
    assert economics.profit == D("3.71")
    assert economics.stress_debit == D("6.30")


def test_spread_economics_fails_roundtrip_gate_when_fees_dominate():
    # 权利金很小时，费用按权利金封顶比例计收，往返成本吞掉借记。
    cheap_long = long_leg(bid=D("100"), ask=D("101"))
    cheap_short = short_leg(bid=D("60"), ask=D("61"))
    economics = spread_economics(cheap_long, cheap_short, D("0.01"), protocol())
    assert economics is not None
    assert economics.eligible is False
    assert "roundtrip_risk" in economics.failed_gates


def test_spread_economics_needs_both_legs_executable():
    assert spread_economics(long_leg(), short_leg(status="Closed"), D("0.01"), protocol()) is None


def test_spread_economics_reuses_paid_long_cost():
    economics = spread_economics(
        long_leg(), short_leg(), D("0.01"), protocol(), paid_long=D("15.095")
    )
    assert economics is not None
    assert economics.long_cost == D("15.095")


# --------------------------------------------------------------------------
# 候选选择与 cohort 预算
# --------------------------------------------------------------------------


def test_choose_candidate_picks_expiry_closest_to_target():
    legs = [
        long_leg(),
        short_leg(),
        long_leg(symbol="BTC-04JAN27-30000-C-USDT", expiry_ms=NOW + 40 * DAY),
        short_leg(symbol="BTC-04JAN27-31000-C-USDT", expiry_ms=NOW + 40 * DAY),
    ]
    candidate = choose_candidate(
        base="BTC", legs=legs, now_ms=NOW, available=D("25"), protocol=protocol()
    )
    assert candidate is not None
    assert candidate.long_leg.symbol == "BTC-21DEC26-30000-C-USDT"
    assert candidate.qty == D("0.03")
    assert candidate.reserved_risk == D("18.90")
    assert candidate.economics.eligible is True


def test_choose_candidate_returns_none_without_eligible_pair():
    assert (
        choose_candidate(
            base="BTC",
            legs=[long_leg(), short_leg(delta=D("0.10"))],
            now_ms=NOW,
            available=D("25"),
            protocol=protocol(),
        )
        is None
    )


def test_choose_candidate_respects_available_budget():
    candidate = choose_candidate(
        base="BTC",
        legs=[long_leg(), short_leg()],
        now_ms=NOW,
        available=D("10"),
        protocol=protocol(),
    )
    assert candidate is not None
    assert candidate.economics.stress_debit <= D("10")


def test_remaining_risk_budget_excludes_one_base():
    reserved = {"BTC": D("10"), "ETH": D("5")}
    assert remaining_risk_budget(reserved, protocol()) == D("10")
    assert remaining_risk_budget(reserved, protocol(), excluding="BTC") == D("20")
    assert remaining_risk_budget({"BTC": D("30")}, protocol()) == D("0")


@pytest.mark.parametrize("value", [D("-1"), D("NaN"), D("Infinity"), None])
def test_remaining_risk_budget_rejects_corrupt_reservations(value):
    with pytest.raises(ValueError):
        remaining_risk_budget({"BTC": value}, protocol())


def test_bear_contract_ok_requires_puts_and_higher_long_strike():
    long_put = long_leg(option_type="Put", strike=D("65000"))
    short_put = short_leg(option_type="Put", strike=D("60000"))
    assert bear_contract_ok(long_put, short_put, D("0.01"), protocol()) is True
    assert bear_contract_ok(long_leg(), short_leg(), D("0.01"), protocol()) is False
    assert bear_contract_ok(short_put, long_put, D("0.01"), protocol()) is False


# --------------------------------------------------------------------------
# 阶段状态机与现金/费用累计
# --------------------------------------------------------------------------


def test_illegal_phase_transition_is_rejected():
    ledger = PositionLedger(base="BTC")
    with pytest.raises(ValueError):
        ledger.transition(StudyPhase.OPEN)


def test_full_lifecycle_accumulates_cash_and_fees():
    ledger = PositionLedger(base="BTC", quantity=D("0.01"))
    long_symbol = "BTC-21DEC26-30000-C-USDT"
    short_symbol = "BTC-21DEC26-31000-C-USDT"

    ledger.set_pending(
        PendingIntent(
            purpose=IntentPurpose.OPEN_LONG,
            symbol=long_symbol,
            side="Buy",
            created_ms=1000,
            decision_source_ms=1000,
            deadline_ms=2000,
        )
    )
    record = ledger.apply_fill(
        purpose=IntentPurpose.OPEN_LONG,
        symbol=long_symbol,
        side="Buy",
        price=D("1500.5"),
        qty=D("0.01"),
        fee=D("0.09"),
        quote_source_ms=1100,
        now_ms=1100,
    )
    assert ledger.phase is StudyPhase.OPENING_SHORT
    assert ledger.cash == D("-15.095")
    assert ledger.fees == D("0.09")
    assert record.latency_seconds == D("0.1")

    ledger.set_pending(
        PendingIntent(
            purpose=IntentPurpose.OPEN_SHORT,
            symbol=short_symbol,
            side="Sell",
            created_ms=1100,
            decision_source_ms=1100,
        )
    )
    ledger.apply_fill(
        purpose=IntentPurpose.OPEN_SHORT,
        symbol=short_symbol,
        side="Sell",
        price=D("889.5"),
        qty=D("0.01"),
        fee=D("0.09"),
        quote_source_ms=1200,
        now_ms=1200,
    )
    assert ledger.phase is StudyPhase.OPEN
    assert ledger.cash == D("-6.29")
    assert ledger.entry_debit == D("6.29")
    assert ledger.reserved_risk == D("6.29")

    value = ledger.net_value(
        {
            long_symbol: ExitQuote(price=D("1489.5"), index_price=D("30000")),
            short_symbol: ExitQuote(price=D("900.5"), index_price=D("30000")),
        },
        protocol(),
    )
    assert value == D("-0.58")

    ledger.set_pending(
        PendingIntent(
            purpose=IntentPurpose.CLOSE_SHORT,
            symbol=short_symbol,
            side="Buy",
            created_ms=1200,
            decision_source_ms=1200,
        )
    )
    ledger.apply_fill(
        purpose=IntentPurpose.CLOSE_SHORT,
        symbol=short_symbol,
        side="Buy",
        price=D("900.5"),
        qty=D("0.01"),
        fee=D("0.09"),
        quote_source_ms=1300,
        now_ms=1300,
    )
    assert ledger.phase is StudyPhase.CLOSING_LONG

    ledger.set_pending(
        PendingIntent(
            purpose=IntentPurpose.CLOSE_LONG,
            symbol=long_symbol,
            side="Sell",
            created_ms=1300,
            decision_source_ms=1300,
        )
    )
    ledger.apply_fill(
        purpose=IntentPurpose.CLOSE_LONG,
        symbol=long_symbol,
        side="Sell",
        price=D("1489.5"),
        qty=D("0.01"),
        fee=D("0.09"),
        quote_source_ms=1400,
        now_ms=1400,
    )
    assert ledger.phase is StudyPhase.CLOSED
    assert ledger.cash == D("-0.58")
    assert ledger.reserved_risk == D("0")
    assert ledger.fees == D("0.36")
    assert ledger.holding is False


def test_fill_requires_a_strictly_newer_quote():
    ledger = PositionLedger(base="BTC", quantity=D("0.01"))
    ledger.set_pending(
        PendingIntent(
            purpose=IntentPurpose.OPEN_LONG,
            symbol="BTC-21DEC26-30000-C-USDT",
            side="Buy",
            created_ms=1000,
            decision_source_ms=1000,
        )
    )
    assert ledger.is_fresh(1001) is True
    assert ledger.is_fresh(1000) is False
    with pytest.raises(ValueError):
        ledger.apply_fill(
            purpose=IntentPurpose.OPEN_LONG,
            symbol="BTC-21DEC26-30000-C-USDT",
            side="Buy",
            price=D("1500.5"),
            qty=D("0.01"),
            fee=D("0.09"),
            quote_source_ms=1000,
            now_ms=1000,
        )


def test_abort_path_closes_only_the_filled_leg():
    ledger = PositionLedger(base="BTC", quantity=D("0.01"))
    long_symbol = "BTC-21DEC26-30000-C-USDT"
    ledger.set_pending(
        PendingIntent(
            purpose=IntentPurpose.OPEN_LONG,
            symbol=long_symbol,
            side="Buy",
            created_ms=1000,
            decision_source_ms=1000,
        )
    )
    ledger.apply_fill(
        purpose=IntentPurpose.OPEN_LONG,
        symbol=long_symbol,
        side="Buy",
        price=D("1500.5"),
        qty=D("0.01"),
        fee=D("0.09"),
        quote_source_ms=1100,
        now_ms=1100,
    )
    ledger.transition(StudyPhase.ABORTING)
    ledger.set_pending(
        PendingIntent(
            purpose=IntentPurpose.CLOSE_LONG,
            symbol=long_symbol,
            side="Sell",
            created_ms=1200,
            decision_source_ms=1200,
        )
    )
    ledger.apply_fill(
        purpose=IntentPurpose.CLOSE_LONG,
        symbol=long_symbol,
        side="Sell",
        price=D("1489.5"),
        qty=D("0.01"),
        fee=D("0.09"),
        quote_source_ms=1300,
        now_ms=1300,
    )
    assert ledger.phase is StudyPhase.ABORTED
    assert ledger.holding is False


def test_expiry_with_open_leg_is_unresolved():
    ledger = PositionLedger(
        base="BTC",
        phase=StudyPhase.OPENING_SHORT,
        quantity=D("0.01"),
        long_held=True,
        reserved_risk=D("10"),
    )
    assert ledger.end_at_expiry() is StudyPhase.EXPIRED_UNRESOLVED
    flat = PositionLedger(base="ETH")
    assert flat.end_at_study_end() is StudyPhase.CANCELLED


# --------------------------------------------------------------------------
# 观察缺口
# --------------------------------------------------------------------------


def test_observation_gap_detected_only_above_the_limit():
    assert detect_gap(base="BTC", previous_ms=0, observed_ms=30_000, max_gap_seconds=30) is None
    gap = detect_gap(base="BTC", previous_ms=0, observed_ms=30_001, max_gap_seconds=30)
    assert gap is not None
    assert gap.gap_seconds == D("30.001")
    with pytest.raises(ValueError, match="严格递增"):
        detect_gap(base="BTC", previous_ms=1000, observed_ms=1000, max_gap_seconds=30)
