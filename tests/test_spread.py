"""双腿价差套利：状态机、风控闸门与瘸腿自愈。"""

from __future__ import annotations

from decimal import Decimal as D

import pytest
from pydantic import ValidationError

from bybitbot.settings.models import DecimalAmount, SpreadPairSettings, SpreadSettings
from bybitbot.spread.execution import (
    ACTION_ALERT,
    ACTION_CANCEL,
    ACTION_CLOSE,
    ACTION_SUBMIT,
    EVENT_DEADLINE,
    EVENT_LEG_FILL,
    EVENT_LEG_TERMINAL,
    EVENT_TICK,
    LEG_A,
    LEG_B,
    LEG_CANCELED,
    ROLE_ACTIVE,
    ROLE_PASSIVE,
    ROLE_RECOVERY,
    LegQuote,
    PairEvent,
    PairSignal,
    new_execution,
    order_link_id,
    reduce,
)
from bybitbot.spread.risk import (
    ACCOUNT_LEVERAGE_EXCEEDED,
    API_COOLDOWN_ACTIVE,
    MM_RATE_TOO_HIGH,
    ORDER_RATE_EXCEEDED,
    PAIR_EQUITY_RATIO_EXCEEDED,
    PairRiskContext,
    PairRiskLimits,
    evaluate_forced_reduce,
    evaluate_funding_pressure,
    evaluate_open,
    evaluate_spread_stop,
    unhedged_breach,
)
from bybitbot.spread.states import (
    ABORTED,
    ERROR_UNHEDGED,
    HEDGED,
    OPENING,
    PairState,
    TransitionError,
    apply_transition,
    equal_notional_qty,
    log_spread,
    quantize_qty,
    unhedged_notional,
    z_score,
)

# --------------------------------------------------------------------------
# 状态机
# --------------------------------------------------------------------------


def test_terminal_pair_state_never_moves_backwards():
    state = PairState(ABORTED)
    moved = apply_transition(state, target=HEDGED)
    assert moved.state == ABORTED


def test_hedged_requires_both_legs_and_a_bounded_exposure():
    # 只成交一条腿就不许标记为已对冲 —— 证据不足时拒绝，而不是猜。
    with pytest.raises(TransitionError, match="两条腿"):
        apply_transition(
            PairState(OPENING), target=HEDGED, filled_qty_a=D("1"), notional_a=D("100")
        )
    with pytest.raises(TransitionError, match="裸敞口"):
        apply_transition(
            PairState(OPENING),
            target=HEDGED,
            filled_qty_a=D("1"),
            filled_qty_b=D("1"),
            notional_a=D("100"),
            notional_b=D("10"),
            max_unhedged_notional=D("50"),
        )


def test_fill_quantity_never_shrinks():
    # 旧快照不倒退已确认的成交：更小的数量被夹住，而不是被采纳。
    moved = apply_transition(
        PairState(OPENING, filled_qty_a=D("5")), target=OPENING, filled_qty_a=D("1")
    )
    assert moved.filled_qty_a == D("5")


def test_equal_notional_hedge_rounds_down_never_up():
    # 等名义价值：1000 USDT / 100 = 10；步长 0.01 时 7.999 只能向下取整。
    assert equal_notional_qty(
        filled_notional_a=D("1000"), price_b=D("100"), qty_step_b=D("0.001")
    ) == D("10")
    assert equal_notional_qty(
        filled_notional_a=D("1000"), price_b=D("100"), qty_step_b=D("0.01")
    ) == D("10")
    assert quantize_qty(D("7.999"), D("0.01")) == D("7.99")


def test_log_spread_and_z_score():
    assert log_spread(D("100"), D("100"), D("1")) == 0
    assert z_score(D("3"), D("2"), D("0.5")) == D("2")
    with pytest.raises(ValueError):
        z_score(D("3"), D("2"), D("0"))


def test_unhedged_notional_is_the_leg_difference():
    state = PairState(OPENING, notional_a=D("100"), notional_b=D("40"))
    assert unhedged_notional(state) == D("60")


# --------------------------------------------------------------------------
# 风控闸门
# --------------------------------------------------------------------------


def _context(**changes) -> PairRiskContext:
    base = {
        "equity": D("1000"),
        "account_notional": D("1000"),
        "mm_rate": D("0.1"),
        "now_ms": 1000,
    }
    base.update(changes)
    return PairRiskContext(**base)


def test_open_is_refused_when_maintenance_margin_is_too_high():
    decision = evaluate_open(
        limits=PairRiskLimits(), context=_context(mm_rate=D("0.6")), pair_notional=D("100")
    )
    assert not decision.allowed
    assert MM_RATE_TOO_HIGH in decision.reasons


def test_pair_notional_cannot_exceed_thirty_percent_of_equity():
    decision = evaluate_open(limits=PairRiskLimits(), context=_context(), pair_notional=D("301"))
    assert not decision.allowed
    assert PAIR_EQUITY_RATIO_EXCEEDED in decision.reasons


def test_total_leverage_cannot_exceed_three_times_equity():
    decision = evaluate_open(
        limits=PairRiskLimits(),
        context=_context(account_notional=D("2500")),
        pair_notional=D("600"),
    )
    assert not decision.allowed
    assert ACCOUNT_LEVERAGE_EXCEEDED in decision.reasons


def test_api_cooldown_and_order_rate_block_new_opens():
    limited = evaluate_open(
        limits=PairRiskLimits(),
        context=_context(api_cooldown_until_ms=5000),
        pair_notional=D("100"),
    )
    assert API_COOLDOWN_ACTIVE in limited.reasons
    busy = evaluate_open(
        limits=PairRiskLimits(), context=_context(order_events=20), pair_notional=D("100")
    )
    assert ORDER_RATE_EXCEEDED in busy.reasons


def test_forced_reduce_only_beyond_three_quarters_margin():
    limits = PairRiskLimits()
    assert not evaluate_forced_reduce(limits=limits, context=_context(mm_rate=D("0.6")))
    assert evaluate_forced_reduce(limits=limits, context=_context(mm_rate=D("0.8")))


def test_unhedged_breach_needs_the_grace_period_to_elapse():
    limits = PairRiskLimits()
    assert not unhedged_breach(
        limits=limits, notional_a=D("100"), notional_b=D("0"), first_breach_ms=1000, now_ms=2000
    )
    assert unhedged_breach(
        limits=limits, notional_a=D("100"), notional_b=D("0"), first_breach_ms=1000, now_ms=6000
    )
    # 未越界时不算 breach。
    assert not unhedged_breach(
        limits=limits, notional_a=D("100"), notional_b=D("90"), first_breach_ms=1000, now_ms=9000
    )


def test_spread_stop_loss_and_funding_pressure():
    limits = PairRiskLimits()
    assert evaluate_spread_stop(limits=limits, z_value=D("3.5"))
    assert not evaluate_spread_stop(limits=limits, z_value=D("1.0"))
    assert evaluate_funding_pressure(
        limits=limits, net_funding_pnl=D("-60"), expected_profit=D("100")
    )
    assert not evaluate_funding_pressure(
        limits=limits, net_funding_pnl=D("-10"), expected_profit=D("100")
    )


# --------------------------------------------------------------------------
# 双腿执行与瘸腿自愈
# --------------------------------------------------------------------------


def signal(**changes) -> PairSignal:
    base = {
        "pair_trade_id": "pt001",
        "pair_key": "BTCUSDT:BTC-26MAR27",
        "strategy_id": "spread-1",
        "symbol_a": "BTCUSDT",
        "symbol_b": "BTC-26MAR27",
        "direction": "SHORT_A_LONG_B",
        "notional": D("1000"),
        "passive_price": D("100"),
        "active_price": D("100"),
        "qty_step_a": D("0.001"),
        "qty_step_b": D("0.001"),
        "side_a": "Buy",
        "side_b": "Sell",
        "passive_timeout_ms": 2000,
        "recovery_window_ms": 500,
        "max_slippage_bps": D("50"),
        "max_unhedged_notional": D("50"),
        "unhedged_grace_ms": 5000,
    }
    base.update(changes)
    return PairSignal(**base)


def test_leg_a_fill_triggers_equal_notional_hedge_on_leg_b():
    execution = new_execution(signal(), now_ms=0)
    execution, actions = reduce(
        execution,
        PairEvent(
            kind=EVENT_LEG_FILL,
            leg=LEG_A,
            qty_filled=D("10"),
            avg_price=D("100"),
            at_ms=100,
        ),
    )
    submits = [a for a in actions if a.kind == ACTION_SUBMIT]
    assert len(submits) == 1
    assert submits[0].leg == LEG_B and submits[0].role == ROLE_ACTIVE
    # 1000 USDT 名义 ÷ 100 = 10 股，等名义价值。
    assert submits[0].qty == D("10")
    assert submits[0].price == D("100")
    assert submits[0].order_link_id == "pt001_B1"


def test_both_legs_filled_moves_the_pair_to_hedged():
    execution = new_execution(signal(), now_ms=0)
    execution, _ = reduce(
        execution,
        PairEvent(
            kind=EVENT_LEG_FILL, leg=LEG_A, qty_filled=D("10"), avg_price=D("100"), at_ms=100
        ),
    )
    execution, _ = reduce(
        execution,
        PairEvent(
            kind=EVENT_LEG_FILL, leg=LEG_B, qty_filled=D("10"), avg_price=D("100"), at_ms=110
        ),
    )
    assert execution.state.state == HEDGED
    assert execution.state.filled_qty_a == D("10")
    assert execution.state.filled_qty_b == D("10")


def test_passive_leg_timeout_without_a_fill_cancels_and_aborts():
    execution = new_execution(signal(), now_ms=0)
    execution, actions = reduce(execution, PairEvent(kind=EVENT_DEADLINE, at_ms=2000))
    cancels = [a for a in actions if a.kind == ACTION_CANCEL]
    assert len(cancels) == 1 and cancels[0].leg == LEG_A and cancels[0].role == ROLE_PASSIVE
    assert execution.state.state == ABORTED


def test_partial_hedge_is_chased_within_the_recovery_window():
    execution = new_execution(signal(), now_ms=0)
    execution, _ = reduce(
        execution,
        PairEvent(
            kind=EVENT_LEG_FILL, leg=LEG_A, qty_filled=D("10"), avg_price=D("100"), at_ms=100
        ),
    )
    execution, actions = reduce(
        execution,
        PairEvent(
            kind=EVENT_LEG_FILL,
            leg=LEG_B,
            qty_filled=D("6"),
            avg_price=D("100"),
            at_ms=120,
            quote_b=LegQuote("BTC-26MAR27", bid=D("100.05"), ask=D("100.15")),
        ),
    )
    chases = [a for a in actions if a.kind == ACTION_SUBMIT and a.role == ROLE_RECOVERY]
    assert len(chases) == 1
    # 缺口 = 10 − 6 = 4 股。
    assert chases[0].qty == D("4")
    assert chases[0].price == D("100.05")


def test_chase_beyond_slippage_tolerance_flattens_the_filled_leg():
    execution = new_execution(signal(), now_ms=0)
    execution, _ = reduce(
        execution,
        PairEvent(
            kind=EVENT_LEG_FILL, leg=LEG_A, qty_filled=D("10"), avg_price=D("100"), at_ms=100
        ),
    )
    execution, actions = reduce(
        execution,
        PairEvent(
            kind=EVENT_LEG_FILL,
            leg=LEG_B,
            qty_filled=D("6"),
            avg_price=D("100"),
            at_ms=120,
            quote_b=LegQuote("BTC-26MAR27", bid=D("110"), ask=D("110.1")),
        ),
    )
    assert any(a.kind == ACTION_ALERT for a in actions)
    closes = [a for a in actions if a.kind == ACTION_CLOSE]
    assert len(closes) == 1 and closes[0].leg == LEG_A
    # 多出来的名义 = 1000 − 600 = 400 → 4 股。
    assert closes[0].qty == D("4")
    assert execution.state.state == ERROR_UNHEDGED


def test_unhedged_exposure_survives_the_grace_period_before_being_closed():
    execution = new_execution(signal(), now_ms=0)
    execution, _ = reduce(
        execution,
        PairEvent(
            kind=EVENT_LEG_FILL, leg=LEG_A, qty_filled=D("10"), avg_price=D("100"), at_ms=100
        ),
    )
    execution, actions = reduce(execution, PairEvent(kind=EVENT_TICK, at_ms=2000))
    assert not [a for a in actions if a.kind == ACTION_CLOSE]
    execution, actions = reduce(execution, PairEvent(kind=EVENT_TICK, at_ms=5100))
    closes = [a for a in actions if a.kind == ACTION_CLOSE]
    assert len(closes) == 1 and closes[0].leg == LEG_A
    assert execution.state.state == ERROR_UNHEDGED


def test_unhedged_timer_resets_once_exposure_returns_within_the_limit():
    execution = new_execution(signal(), now_ms=0)
    execution, _ = reduce(
        execution,
        PairEvent(
            kind=EVENT_LEG_FILL, leg=LEG_A, qty_filled=D("10"), avg_price=D("100"), at_ms=100
        ),
    )
    execution, _ = reduce(
        execution,
        PairEvent(
            kind=EVENT_LEG_FILL, leg=LEG_B, qty_filled=D("10"), avg_price=D("100"), at_ms=110
        ),
    )
    assert execution.state.state == HEDGED
    assert execution.first_unhedged_ms is None


def test_rejected_hedge_leg_is_chased_instead_of_silently_accepted():
    execution = new_execution(signal(), now_ms=0)
    execution, _ = reduce(
        execution,
        PairEvent(
            kind=EVENT_LEG_FILL, leg=LEG_A, qty_filled=D("10"), avg_price=D("100"), at_ms=100
        ),
    )
    execution, actions = reduce(
        execution,
        PairEvent(
            kind=EVENT_LEG_TERMINAL,
            leg=LEG_B,
            status=LEG_CANCELED,
            at_ms=120,
            quote_b=LegQuote("BTC-26MAR27", bid=D("100"), ask=D("100.2")),
        ),
    )
    chases = [a for a in actions if a.kind == ACTION_SUBMIT and a.role == ROLE_RECOVERY]
    assert len(chases) == 1 and chases[0].qty == D("10")


def test_order_link_id_is_stable_per_logical_order_and_unique_per_sequence():
    assert order_link_id(pair_trade_id="pt001", leg=LEG_A, sequence=0) == "pt001_A0"
    assert order_link_id(pair_trade_id="pt001", leg=LEG_B, sequence=1) == "pt001_B1"
    # 同一笔逻辑订单的重试必须复用同一个编号。
    assert order_link_id(pair_trade_id="pt001", leg=LEG_A, sequence=0) == "pt001_A0"
    # Bybit 的 orderLinkId 上限是 36 个字符，超长必须被拒绝。
    with pytest.raises(ValueError):
        order_link_id(pair_trade_id="x" * 40, leg=LEG_A, sequence=0)


# --------------------------------------------------------------------------
# 配置校验
# --------------------------------------------------------------------------


def pair_settings(**changes):
    base = {
        "pair_key": "BTCUSDT:BTC-26MAR27",
        "symbol_a": "BTCUSDT",
        "symbol_b": "BTC-26MAR27",
        "hedge_ratio": D("1"),
        "z_entry": D("2"),
        "z_exit": D("0.5"),
        "z_stop": D("3.5"),
        "max_notional": DecimalAmount(value=D("300"), currency="USDT"),
        "max_unhedged_notional": DecimalAmount(value=D("50"), currency="USDT"),
    }
    base.update(changes)
    return SpreadPairSettings(**base)


def test_valid_pair_settings_are_accepted():
    pair = pair_settings()
    assert pair.symbol_a == "BTCUSDT"
    assert pair.max_notional.currency == "USDT"


@pytest.mark.parametrize(
    "changes,message",
    [
        ({"z_entry": D("0.5"), "z_exit": D("2")}, "z_exit < z_entry < z_stop"),
        ({"z_stop": D("1")}, "z_exit < z_entry < z_stop"),
        ({"pair_key": "BTCUSDT:ETHUSDT"}, "pair_key"),
        ({"symbol_b": "BTCUSDT"}, "不能是同一个合约"),
        ({"max_unhedged_notional": DecimalAmount(value=D("400"), currency="USDT")}, "裸敞口上限"),
        (
            {"max_unhedged_notional": DecimalAmount(value=D("50"), currency="USDC")},
            "同一币种",
        ),
        ({"hedge_ratio": D("0")}, "对冲系数"),
        ({"hedge_ratio": D("NaN")}, "finite_number"),
        ({"z_entry": D("NaN")}, "finite_number"),
        ({"max_slippage_bps": D("Infinity")}, "finite_number"),
        ({"symbol_a": "BTC-USDT"}, "U 本位合约符号"),
    ],
)
def test_invalid_pair_settings_are_rejected(changes, message):
    with pytest.raises(ValidationError, match=message):
        pair_settings(**changes)


def test_spread_subsystem_requires_an_enabled_pair_to_be_turned_on():
    pair = pair_settings()
    with pytest.raises(ValidationError, match="至少要启用一个配对"):
        SpreadSettings(enabled=True, pairs=(pair,))
    enabled = pair_settings(enabled=True)
    settings = SpreadSettings(enabled=True, pairs=(enabled,))
    assert settings.max_concurrent_pairs == 3
    assert settings.max_pair_equity_ratio == D("0.30")


def test_duplicate_pair_keys_are_rejected():
    with pytest.raises(ValidationError, match="不能重复"):
        SpreadSettings(pairs=(pair_settings(), pair_settings()))
