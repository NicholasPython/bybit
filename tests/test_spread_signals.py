"""阶段三滑动窗口信号的确定性单测（纯计算，不访问 DB/网络）。"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from decimal import Decimal as D

import pytest

from bybitbot.spread.signals import (
    ACTION_CLOSE,
    ACTION_NONE,
    ACTION_OPEN,
    ACTION_STOP,
    DIRECTION_LONG_A_SHORT_B,
    DIRECTION_SHORT_A_LONG_B,
    REASON_COST_FILTER_NOT_PASSED,
    REASON_ENTRY_THRESHOLD_REACHED,
    REASON_EXIT_THRESHOLD_REACHED,
    REASON_NO_SIGNAL,
    REASON_STOP_LOSS_TRIGGERED,
    PriceSample,
    SignalPolicy,
    SignalUnavailable,
    evaluate_signal,
    expected_cost,
    log_spread_value,
    rolling_stats,
)

BETA = D(1)
NOTIONAL = D("1000000")


def policy(**overrides):
    base = {
        "window": 5,
        "entry_z": D("1.0"),
        "exit_z": D("0.3"),
        "stop_z": D("2.0"),
        "fee_rate": D("0.0003"),
        "slippage_bps": D("2"),
        "funding_buffer": D("0"),
    }
    base.update(overrides)
    return SignalPolicy(**base)


def samples(prices_a, prices_b=None, *, start_ms=0, step_ms=1000, funding=D(0)):
    prices_b = ["100"] * len(prices_a) if prices_b is None else prices_b
    return [
        PriceSample(
            start_ms + index * step_ms,
            D(price_a) if isinstance(price_a, str) else price_a,
            D(price_b) if isinstance(price_b, str) else price_b,
            funding,
            funding,
        )
        for index, (price_a, price_b) in enumerate(zip(prices_a, prices_b, strict=True))
    ]


def evaluate(window, *, current_policy=None, notional=NOTIONAL, now_ms=None):
    current_policy = current_policy or policy()
    reference = now_ms if now_ms is not None else window[-1].at_ms
    return evaluate_signal(
        window,
        current_policy,
        hedge_ratio=BETA,
        notional=notional,
        now_ms=reference,
    )


def test_normal_open_short_a_long_b_for_positive_z():
    # 前两根相同，第三根 A 大涨：A 高估，做空 A 做多 B。
    decision = evaluate(samples(["99", "100", "100", "100", "103"]))
    assert decision.action == ACTION_OPEN
    assert decision.direction == DIRECTION_SHORT_A_LONG_B
    assert decision.z > 0
    assert decision.reason == REASON_ENTRY_THRESHOLD_REACHED
    assert decision.spread > decision.mean
    assert decision.stdev > 0


def test_normal_open_long_a_short_b_for_negative_z():
    # A 大跌：A 低估，做多 A 做空 B。
    decision = evaluate(samples(["101", "100", "100", "100", "97"]))
    assert decision.action == ACTION_OPEN
    assert decision.direction == DIRECTION_LONG_A_SHORT_B
    assert decision.z < 0


def test_close_when_abs_z_within_exit_band():
    # 窗口末尾靠近均值但仍有波动：|z| 在退出带内，返回 CLOSE。
    no_cost = policy(window=5, fee_rate=D(0), slippage_bps=D(0), exit_z=D("0.5"))
    decision = evaluate(samples(["100", "101", "102", "101", "100.7"]), current_policy=no_cost)
    assert decision.action == ACTION_CLOSE
    assert decision.reason == REASON_EXIT_THRESHOLD_REACHED
    assert decision.direction == ""


def test_stop_takes_priority_over_open():
    # 窗口末尾极端跳升：|z| 超过 stop_z，同时满足 entry_z，必须优先 STOP。
    stopped = evaluate(
        samples(["100", "99", "101", "98", "103"]),
        current_policy=policy(window=5, fee_rate=D(0), slippage_bps=D(0), stop_z=D("1.5")),
    )
    assert stopped.action == ACTION_STOP
    assert stopped.reason == REASON_STOP_LOSS_TRIGGERED
    assert stopped.direction == ""
    assert abs(stopped.z) >= D("1.5")


def test_no_signal_between_exit_and_entry():
    # |z| 落在退出带与开仓阈值之间：既不交易也不平仓。
    no_cost = policy(fee_rate=D(0), slippage_bps=D(0))
    decision = evaluate(samples(["100", "101.2", "100", "101.2", "100"]), current_policy=no_cost)
    assert decision.action == ACTION_NONE
    assert decision.reason == REASON_NO_SIGNAL


def test_cost_filter_blocks_even_threshold_crossing():
    expensive = policy(fee_rate=D("0.05"))
    decision = evaluate(samples(["99", "100", "100", "100", "103"]), current_policy=expensive)
    assert decision.action == ACTION_NONE
    assert decision.reason == REASON_COST_FILTER_NOT_PASSED
    assert decision.expected_cost > 0


def test_cost_filter_boundary_is_inclusive():
    # 零成本、零波动没有可用信号。
    zero_cost = policy(fee_rate=D(0), slippage_bps=D(0))
    flat = samples(["100", "100", "100"])
    with pytest.raises(SignalUnavailable):
        evaluate(flat, current_policy=zero_cost)  # 标准差为零，先不可用

    # 把 funding_buffer 精准设成偏离收益：gain == cost 时也必须过滤。
    window = samples(["99", "100", "100", "100", "103"])
    mean, _ = rolling_stats([log_spread_value(sample, BETA) for sample in window], 5)
    gain = abs(log_spread_value(window[-1], BETA) - mean) * NOTIONAL
    exact_cost = policy(funding_buffer=gain)
    decision = evaluate(window, current_policy=exact_cost)
    assert decision.action == ACTION_NONE
    assert decision.reason == REASON_COST_FILTER_NOT_PASSED


def test_insufficient_samples_raise():
    with pytest.raises(SignalUnavailable, match="样本不足"):
        evaluate(samples(["100", "100"]))


def test_stale_sample_raises():
    window = samples(["99", "100", "100", "100", "103"])
    with pytest.raises(SignalUnavailable, match="过期"):
        evaluate(window, now_ms=window[-1].at_ms + 30001)


def test_stale_boundary_is_accepted():
    window = samples(["99", "100", "100", "100", "103"])
    assert evaluate(window, now_ms=window[-1].at_ms + 30000).action == ACTION_OPEN


def test_duplicate_and_out_of_order_timestamps_raise():
    with pytest.raises(SignalUnavailable, match="严格递增"):
        evaluate(
            [
                PriceSample(0, D("100"), D("100")),
                PriceSample(1000, D("100"), D("100")),
                PriceSample(2000, D("103"), D("100")),
                PriceSample(3000, D("100"), D("100")),
                PriceSample(1000, D("100"), D("100")),
            ]
        )
    with pytest.raises(SignalUnavailable, match="严格递增"):
        evaluate(
            [
                PriceSample(0, D("100"), D("100")),
                PriceSample(2000, D("100"), D("100")),
                PriceSample(3000, D("103"), D("100")),
                PriceSample(4000, D("100"), D("100")),
                PriceSample(1000, D("100"), D("100")),
            ]
        )


def test_zero_stdev_raises():
    with pytest.raises(SignalUnavailable, match="标准差"):
        rolling_stats([D("2"), D("2"), D("2"), D("2"), D("2")], 5)
    with pytest.raises(SignalUnavailable, match="标准差"):
        evaluate(samples(["100", "100", "100", "100", "100"]))


def test_expected_cost_formula():
    # 2*10000*0.001 + 2*10000*5/10000 + 3 = 33
    assert expected_cost(D("10000"), D("0.001"), D("5"), D("3")) == D("33")
    assert expected_cost(D(0), D("1"), D("10000"), D("7")) == D("7")
    with pytest.raises(ValueError):
        expected_cost(D("-1"), D(0), D(0), D(0))
    with pytest.raises(ValueError):
        expected_cost(D(1), D("NaN"), D(0), D(0))


def test_log_spread_value_uses_states_formula():
    from bybitbot.spread.states import log_spread

    sample = PriceSample(0, D("110"), D("100"), D("0.01"), D("-0.01"))
    assert log_spread_value(sample, D("0.5")) == D("110").ln() - D("0.5") * D("100").ln()
    assert log_spread_value(sample, D("1")) == log_spread(D("110"), D("100"), D(1))
    for beta in (D(0), D("-1"), D("Infinity"), D("NaN")):
        with pytest.raises(ValueError, match="hedge_ratio"):
            log_spread_value(sample, beta)


def test_rolling_stats_uses_population_stdev_and_last_window():
    mean, stdev = rolling_stats([D("1"), D("2"), D("3"), D("4")], 3)
    assert mean == D(3)
    assert stdev == (D("2") / D(3)).sqrt()
    with pytest.raises(ValueError):
        rolling_stats([D("1"), D("2"), D("3")], 1)
    with pytest.raises(SignalUnavailable):
        rolling_stats([D("1"), D("NaN"), D("3"), D("4"), D("5")], 5)


@pytest.mark.parametrize(
    "overrides",
    [
        {"window": 1},
        {"window": 0},
        {"window": True},
        {"window": D(3)},
        {"exit_z": D("-0.1")},
        {"exit_z": D("1.0")},
        {"entry_z": D("2.0")},
        {"stop_z": D("0.5")},
        {"fee_rate": D("-0.001")},
        {"slippage_bps": D("-1")},
        {"funding_buffer": D("-1")},
        {"funding_buffer": D("NaN")},
        {"entry_z": D("Infinity")},
        {"max_signal_age_ms": -1},
        {"max_signal_age_ms": D(30000)},
    ],
)
def test_invalid_policy_parameters_raise(overrides):
    with pytest.raises(ValueError):
        policy(**overrides)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"price_a": D(0)},
        {"price_b": D("-100")},
        {"price_a": D("NaN")},
        {"price_b": D("Infinity")},
        {"funding_a": D("NaN")},
        {"funding_b": D("Infinity")},
        {"at_ms": -1},
        {"at_ms": D(0)},
        {"at_ms": True},
    ],
)
def test_invalid_sample_parameters_raise(kwargs):
    valid = {"at_ms": 0, "price_a": D("100"), "price_b": D("100")}
    valid.update(kwargs)
    with pytest.raises(ValueError):
        PriceSample(**valid)


def test_dataclasses_are_frozen():
    frozen = policy()
    with pytest.raises(FrozenInstanceError):
        frozen.window = 4
    sample = PriceSample(0, D("100"), D("100"))
    with pytest.raises(FrozenInstanceError):
        sample.price_a = D("101")


def test_invalid_evaluation_inputs_raise():
    window = samples(["99", "100", "100", "100", "103"])
    with pytest.raises(ValueError, match="notional"):
        evaluate(window, notional=D("-1"))
    with pytest.raises(ValueError, match="hedge_ratio"):
        evaluate_signal(
            window, policy(), hedge_ratio=D(0), notional=NOTIONAL, now_ms=window[-1].at_ms
        )
    with pytest.raises(ValueError, match="now_ms"):
        evaluate(window, now_ms=D(2000))
