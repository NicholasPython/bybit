"""冻结研究协议、完整 K 线信号的边界与坏输入回归；全程离线。"""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, fields
from decimal import Decimal
from pathlib import Path

import pytest

from bybitbot.validation.indicators import SignalPolicy, evaluate_signal, sma
from bybitbot.validation.protocol import DAY_MS, HOUR_MS, SpreadProtocol

D = Decimal
ROOT = Path(__file__).resolve().parents[1]
DECIMAL_FIELDS = [f.name for f in fields(SpreadProtocol) if isinstance(f.default, Decimal)]
INTEGER_FIELDS = [f.name for f in fields(SpreadProtocol) if isinstance(f.default, int)]


def candles(closes, width):
    return [(index * width, close) for index, close in enumerate(closes)]


def bull_history():
    return [D(100)] * 22 + [D(130)] * 3, [D(100)] * 50 + [D(120)]


def signal(hours, days, policy=SignalPolicy.STRONG_UP):
    return evaluate_signal(
        hourly=candles(hours, HOUR_MS), daily=candles(days, DAY_MS), policy=policy
    )


def test_frozen_bull_protocol_parses_and_remains_immutable():
    path = ROOT / "research/2026-11-15-bull-call-forward/run-v1/frozen/protocol.json"
    data = json.loads(path.read_text())
    protocol = SpreadProtocol.from_mapping(data)
    assert protocol == SpreadProtocol()
    for name in DECIMAL_FIELDS:
        assert getattr(protocol, name) == D(data[name])
    for name in INTEGER_FIELDS:
        assert getattr(protocol, name) == data[name]
    assert protocol.entry_window_ms == (10 * DAY_MS, 40 * DAY_MS)
    assert protocol.target_expiry_offset_ms == 21 * DAY_MS
    with pytest.raises(FrozenInstanceError):
        protocol.sampling_seconds = 600


def test_protocol_normalizes_numbers_in_both_construction_paths():
    data = {"sampling_seconds": "600", "long_delta_min": 0.5, "taker_rate": "0.001"}
    for protocol in (SpreadProtocol.from_mapping(data), SpreadProtocol(**data)):
        assert protocol.sampling_seconds == 600
        assert type(protocol.sampling_seconds) is int
        assert protocol.long_delta_min == D("0.5")
        assert isinstance(protocol.long_delta_min, Decimal)
        assert protocol.taker_rate == D("0.001")


@pytest.mark.parametrize("name", DECIMAL_FIELDS)
@pytest.mark.parametrize(
    "value",
    [None, True, "bad", "NaN", "sNaN", "Infinity", "-Infinity", D("NaN"), float("inf"), [], {}],
)
def test_protocol_rejects_malformed_decimals(name, value):
    for constructor in (SpreadProtocol.from_mapping, lambda data: SpreadProtocol(**data)):
        with pytest.raises(ValueError, match=name):
            constructor({name: value})


@pytest.mark.parametrize("name", INTEGER_FIELDS)
@pytest.mark.parametrize("value", [None, True, "bad", "1.1", 1.1, 900.0, "NaN", float("inf")])
def test_protocol_rejects_invalid_integers_without_truncation(name, value):
    for constructor in (SpreadProtocol.from_mapping, lambda data: SpreadProtocol(**data)):
        with pytest.raises(ValueError, match=name):
            constructor({name: value})


@pytest.mark.parametrize("name", DECIMAL_FIELDS + INTEGER_FIELDS)
def test_protocol_rejects_negative_thresholds(name):
    with pytest.raises(ValueError, match=name):
        SpreadProtocol.from_mapping({name: -1})


@pytest.mark.parametrize(
    "data",
    [
        {"sampling_seconds": 0},
        {"study_days": 0},
        {"aggregate_risk_budget_usdt": 0},
        {"max_first_leg_outlay_usdt": 0},
        {"width_index_min": 0},
        {"min_reward_risk": 0},
        {"take_profit_debit_fraction": 0},
        {"long_delta_min": "0.60"},
        {"target_long_delta": "0.70"},
        {"short_delta_max": "0.30"},
        {"target_short_delta": "1.1"},
        {"long_delta_min": "1.1", "target_long_delta": "1.2", "long_delta_max": "1.3"},
        {"min_entry_days": 22},
        {"max_entry_days": 20},
        {"width_index_min": "0.09"},
        {"width_index_max": "1"},
        {"max_spread_fraction": "1.01"},
        {"entry_limit_fraction": "1.01"},
        {"taker_rate": "1.01"},
        {"premium_fee_cap": "1.01"},
        {"max_entry_friction_profit": "1.01"},
        {"max_roundtrip_loss_risk": "1.01"},
        {"max_roundtrip_loss_profit": "1.01"},
        {"stop_loss_debit_fraction": "1.01"},
        {"adverse_slippage_ticks": 3},
        {"max_observation_gap_seconds": 899},
        {"pending_ttl_seconds": 899},
        {"max_response_age_seconds": 901},
        {"end_exit_buffer_seconds": 899},
        {"end_exit_buffer_seconds": 5 * 86400},
        {"close_before_expiry_hours": 240},
    ],
)
def test_protocol_rejects_incoherent_ranges_and_timing(data):
    with pytest.raises(ValueError):
        SpreadProtocol.from_mapping(data)


def test_protocol_accepts_inclusive_range_and_timing_boundaries():
    protocol = SpreadProtocol(
        min_entry_days=21,
        max_entry_days=21,
        long_delta_min=D("0.55"),
        long_delta_max=D("0.55"),
        short_delta_min=D("0.35"),
        short_delta_max=D("0.35"),
        width_index_min=D("0.08"),
        adverse_slippage_ticks=2,
        stress_adverse_ticks=2,
        max_observation_gap_seconds=900,
        pending_ttl_seconds=900,
        max_response_age_seconds=900,
        end_exit_buffer_seconds=900,
    )
    assert protocol.entry_window_ms == (21 * DAY_MS, 21 * DAY_MS)
    assert (
        SpreadProtocol(adverse_slippage_ticks=0, stress_adverse_ticks=0).stress_adverse_ticks == 0
    )
    assert SpreadProtocol(taker_rate=0, premium_fee_cap=0).taker_rate == D(0)
    assert SpreadProtocol(min_reward_risk=2, take_profit_debit_fraction=2).min_reward_risk == D(2)


def test_bear_protocol_is_explicitly_rejected_instead_of_using_bull_budgets():
    path = ROOT / "research/2026-11-15-options-forward/run-v1/protocol.json"
    data = json.loads(path.read_text())
    with pytest.raises(ValueError, match="bear.*incompatible"):
        SpreadProtocol.from_mapping(data)
    data.pop("version")
    with pytest.raises(ValueError, match="incompatible"):
        SpreadProtocol.from_mapping(data)


@pytest.mark.parametrize(
    "name",
    [
        "max_spread_fracton",
        "max_entry_friction_proft",
        "sampling_second",
        "stress_adverse_tick",
        "max_debit_per_trade_usdt",
        "max_long_premium_per_trade_usdt",
        "positions",
        "signal",
    ],
)
def test_protocol_does_not_ignore_unknown_decision_fields(name):
    with pytest.raises(ValueError, match="unknown or incompatible"):
        SpreadProtocol.from_mapping({name: 1})


@pytest.mark.parametrize("data", [None, [], {"mode": "live"}, {"version": "unknown"}])
def test_protocol_rejects_bad_mapping_or_mode(data):
    with pytest.raises(ValueError):
        SpreadProtocol.from_mapping(data)


def test_protocol_ignores_metadata():
    assert SpreadProtocol.from_mapping({"notes": "offline only", "metadata": {"owner": "test"}})


def test_sma_uses_last_window_and_validates_window():
    assert sma([D(1), D(2), D(3)], 2) == D("2.5")
    assert sma([D(1), D(2), D(3)], 3) == D(2)
    for window in (0, -1, True, 1.5, "2", 4):
        with pytest.raises(ValueError):
            sma([D(1), D(2), D(3)], window)


@pytest.mark.parametrize(
    "policy,minimum", [(SignalPolicy.STRONG_UP, 51), (SignalPolicy.WEAK_DOWN, 50)]
)
def test_exact_history_minimums_and_completed_end_boundaries(policy, minimum):
    hours, days = bull_history()
    days = days[-minimum:]
    result = signal(hours, days, policy)
    assert result.reason != "insufficient_daily_history"
    assert result.last_hour_ms == 25 * HOUR_MS
    assert result.last_day_ms == minimum * DAY_MS
    short_hours = signal(hours[:-1], days, policy)
    assert short_hours.reason == "insufficient_hourly_history"
    assert short_hours.eligible is False
    short_days = signal(hours, days[:-1], policy)
    assert short_days.reason == "insufficient_daily_history"
    assert short_days.eligible is False


@pytest.mark.parametrize("policy", list(SignalPolicy))
def test_policy_enum_and_value_have_identical_results(policy):
    hours, days = bull_history()
    result = signal(hours, days, policy.value)
    assert result == signal(hours, days, policy)
    assert result.policy is policy


@pytest.mark.parametrize("policy", [None, "unknown", "STRONG_UP", "", 0, True, []])
def test_unsupported_policy_is_rejected_even_without_history(policy):
    with pytest.raises(ValueError, match="unsupported signal policy"):
        evaluate_signal(hourly=[], daily=[], policy=policy)


def test_bull_signal_matches_frozen_research_formula():
    hours, days = bull_history()
    result = signal(hours, days)
    assert result.eligible is True
    assert result.reason == "strong_up"
    assert result.hour_close == D(130)
    assert result.daily_close == D(120)
    assert result.sma20 == D(101)
    assert result.previous_sma20 == D(100)
    assert result.sma50 == D("100.4")
    assert result.return_24h == D("0.3")
    assert result.last3_hourly_closes == (D(130),) * 3


def test_bull_allows_flat_sma20_but_rejects_decreasing_sma20():
    hours, days = bull_history()
    days[-21] = D(120)
    flat = signal(hours, days)
    assert flat.sma20 == flat.previous_sma20
    assert flat.eligible is True
    days[-21] = D(121)
    falling = signal(hours, days)
    assert falling.sma20 < falling.previous_sma20
    assert falling.eligible is False


def test_bull_requires_daily_close_strictly_above_sma20():
    hours, _ = bull_history()
    result = signal(hours, [D(100)] * 51)
    assert result.daily_close == result.sma20
    assert result.eligible is False
    assert result.reason == "trend_not_confirmed"


@pytest.mark.parametrize("index", [-3, -2, -1])
@pytest.mark.parametrize("average", ["sma20", "sma50"])
def test_bull_each_of_last_three_closes_must_strictly_exceed_both_averages(index, average):
    hours, days = bull_history()
    if average == "sma50":
        days = [D(200)] * 30 + [D(120)] + [D(100)] * 19 + [D(120)]
        hours[-3:] = [D(200)] * 3
    assert signal(hours, days).eligible is True
    threshold = getattr(signal(hours, days), average)
    hours[index] = threshold
    assert signal(hours, days).eligible is False
    hours[index] += D("0.01")
    assert signal(hours, days).eligible is True


def test_bull_return_must_be_strictly_positive_and_uses_25th_last_hour():
    hours, days = bull_history()
    hours[0] = D(130)
    assert signal(hours, days).return_24h == 0
    assert signal(hours, days).eligible is False
    hours[0] = D(131)
    assert signal(hours, days).eligible is False
    hours[0] = D(100)
    result = signal([D(1)] + hours, days)
    assert result.return_24h == D("0.3")
    assert result.eligible is True


def test_bear_signal_matches_research_formula_at_50_days():
    result = signal([D(100)] * 24 + [D(50)], [D(100)] * 50, SignalPolicy.WEAK_DOWN)
    assert result.eligible is True
    assert result.reason == "weak_down"
    assert result.return_24h == D("-0.5")
    assert result.sma20 == result.sma50 == D(100)
    assert result.previous_sma20 is None
    assert result.last3_hourly_closes == ()


@pytest.mark.parametrize(
    "days,close,average",
    [
        ([D(120)] * 30 + [D(100)] * 20, D(100), "sma20"),
        ([D(80)] * 30 + [D(100)] * 20, D(88), "sma50"),
    ],
)
def test_bear_close_must_be_strictly_below_each_average(days, close, average):
    hours = [D(150)] * 24 + [close]
    result = signal(hours, days, SignalPolicy.WEAK_DOWN)
    assert result.hour_close == getattr(result, average)
    assert result.eligible is False
    hours[-1] -= D("0.01")
    assert signal(hours, days, SignalPolicy.WEAK_DOWN).eligible is True


def test_bear_return_must_be_strictly_negative():
    hours = [D(50)] * 25
    days = [D(100)] * 50
    assert signal(hours, days, SignalPolicy.WEAK_DOWN).eligible is False
    hours[-1] = D(51)
    assert signal(hours, days, SignalPolicy.WEAK_DOWN).eligible is False
    hours[-1] = D(49)
    assert signal(hours, days, SignalPolicy.WEAK_DOWN).eligible is True


@pytest.mark.parametrize(
    "close",
    [
        D(0),
        D(-1),
        D("NaN"),
        D("sNaN"),
        D("Infinity"),
        D("-Infinity"),
        None,
        True,
        "100",
        100,
        100.0,
    ],
)
@pytest.mark.parametrize("series", ["hourly", "daily"])
def test_signals_reject_invalid_closes_including_unused_history(close, series):
    hours, days = bull_history()
    data = {"hourly": candles(hours, HOUR_MS), "daily": candles(days, DAY_MS)}
    data[series][0] = (0, close)
    with pytest.raises(ValueError, match="收盘价"):
        evaluate_signal(**data, policy=SignalPolicy.STRONG_UP)
    with pytest.raises(ValueError, match="收盘价"):
        evaluate_signal(hourly=[(0, close)], daily=[], policy=SignalPolicy.WEAK_DOWN)
    with pytest.raises(ValueError, match="收盘价"):
        sma([close, D(100)], 1)


@pytest.mark.parametrize("series,width", [("hourly", HOUR_MS), ("daily", DAY_MS)])
@pytest.mark.parametrize(
    "kind",
    [
        "duplicate",
        "gap",
        "descending",
        "misaligned",
        "seconds",
        "negative",
        "float",
        "string",
        "bool",
        "short_row",
        "long_row",
        "null_row",
        "null_history",
    ],
)
def test_signals_reject_broken_millisecond_history(series, width, kind):
    hours, days = bull_history()
    data = {"hourly": candles(hours, HOUR_MS), "daily": candles(days, DAY_MS)}
    history = data[series]
    if kind == "duplicate":
        history[1] = history[0]
    elif kind == "gap":
        history.pop(1)
    elif kind == "descending":
        history.reverse()
    elif kind == "misaligned":
        data[series] = [(timestamp + 1, close) for timestamp, close in history]
    elif kind == "seconds":
        data[series] = [(timestamp // 1000, close) for timestamp, close in history]
    elif kind == "negative":
        history[0] = (-width, D(100))
    elif kind == "float":
        history[0] = (0.0, D(100))
    elif kind == "string":
        history[0] = ("0", D(100))
    elif kind == "bool":
        history[0] = (False, D(100))
    elif kind == "short_row":
        history[0] = (0,)
    elif kind == "long_row":
        history[0] = (0, D(100), 1)
    elif kind == "null_row":
        history[0] = None
    else:
        data[series] = None
    with pytest.raises(ValueError):
        evaluate_signal(**data, policy=SignalPolicy.STRONG_UP)
