"""Pure spread regressions; fixtures use USDT premiums and Decimal quantities."""

from __future__ import annotations

import importlib.util
from dataclasses import asdict, replace
from decimal import Decimal
from itertools import permutations
from pathlib import Path

import pytest

from bybitbot.validation.protocol import DAY_MS, SpreadProtocol
from bybitbot.validation.spread import (
    SpreadLeg,
    bear_contract_ok,
    choose_candidate,
    executable_price,
    limit_price,
    remaining_risk_budget,
    spread_economics,
    structure_ok,
)

D = Decimal
NOW = 1_760_000_000_000
PROTOCOL = SpreadProtocol()
BAD_NUMBERS = [D("NaN"), D("sNaN"), D("Infinity"), D("-Infinity"), "bad", None]


def pair(direction="Call"):
    long = SpreadLeg(
        symbol=f"BTC-21DEC26-30000-{direction[0]}-USDT",
        base="BTC",
        status="Trading",
        option_type=direction,
        expiry_ms=NOW + 21 * DAY_MS,
        strike=D("30000") if direction == "Call" else D("31000"),
        index_price=D("30000"),
        bid=D("1490"),
        ask=D("1500"),
        bid_size=D("10"),
        ask_size=D("10"),
        tick_size=D("0.5"),
        qty_step=D("0.01"),
        min_qty=D("0.01"),
        max_qty=D("1"),
        min_price=D("0.01"),
        max_price=D("100000"),
        delta=D("0.55") if direction == "Call" else D("-0.55"),
    )
    short = replace(
        long,
        symbol=f"BTC-21DEC26-31000-{direction[0]}-USDT",
        strike=D("31000") if direction == "Call" else D("30000"),
        bid=D("890"),
        ask=D("900"),
        delta=D("0.35") if direction == "Call" else D("-0.35"),
    )
    return long, short


def choose(legs, *, direction="Call", available=D("25"), protocol=PROTOCOL):
    return choose_candidate(
        base="BTC",
        legs=legs,
        now_ms=NOW,
        available=available,
        protocol=protocol,
        direction=direction,
    )


@pytest.mark.parametrize("direction", ["Call", "Put"])
def test_debit_spread_arithmetic_and_selection(direction):
    long, short = pair(direction)
    assert structure_ok(long, short, NOW, PROTOCOL, direction=direction)
    result = spread_economics(long, short, D("0.01"), PROTOCOL, direction=direction)
    assert result is not None and result.eligible
    assert result.failed_gates == ()
    assert result.long_cost == D("15.095")
    assert result.debit == D("6.29")
    assert result.profit == D("3.71")  # 1000 strike width * .01 qty - debit
    assert result.stress_debit == D("6.30")
    assert result.stress_profit == D("3.70")
    assert result.closing_cash == D("5.71")
    assert result.roundtrip == D("0.58")
    assert result.mid == D("6")
    selected = choose([long, short], direction=direction)
    assert selected is not None
    assert selected.qty == D("0.03")
    assert selected.reserved_risk == D("18.90")
    assert selected.economics.long_cost == D("45.285")
    assert selected.long_limit == D("1530")
    assert selected.short_limit == D("872")


def test_default_api_is_call():
    long, short = pair()
    assert structure_ok(long, short, NOW, PROTOCOL)
    assert spread_economics(long, short, D("0.01"), PROTOCOL) == spread_economics(
        long, short, D("0.01"), PROTOCOL, direction="Call"
    )
    assert choose_candidate(
        base="BTC", legs=[long, short], now_ms=NOW, available=D("25"), protocol=PROTOCOL
    ) == choose([long, short])
    assert choose(pair("Put")) is None


@pytest.mark.parametrize("direction", ["Call", "Put"])
@pytest.mark.parametrize(
    "overrides",
    [
        {"bid": D("1400"), "ask": D("1600")},  # entry spread gate only
        {"max_price": D("1500")},  # ordinary buy exceeds price ceiling
        {"max_price": D("1500.5")},  # only stress buy exceeds price ceiling
    ],
)
def test_paid_long_does_not_require_a_fresh_long_entry(direction, overrides):
    long, short = pair(direction)
    long = replace(long, **overrides)
    assert executable_price(long, "Sell", D("0.01"), PROTOCOL) is not None
    assert spread_economics(long, short, D("0.01"), PROTOCOL, direction=direction) is None
    result = spread_economics(
        long, short, D("0.01"), PROTOCOL, paid_long=D("15.095"), direction=direction
    )
    assert result is not None
    assert result.long_cost == D("15.095")
    assert result.debit == D("6.29")
    assert result.stress_debit == D("6.295")  # paid cost stays fixed under stress
    assert result.profit == D("3.71")
    assert result.stress_profit == D("3.705")
    if "bid" in overrides:
        assert not result.gates["roundtrip_risk"]  # current exit still drives friction


@pytest.mark.parametrize("overrides", [{"status": "Closed"}, {"bid_size": D("0")}])
def test_paid_long_still_requires_executable_exit(overrides):
    long, short = pair()
    assert (
        spread_economics(
            replace(long, **overrides), short, D("0.01"), PROTOCOL, paid_long=D("15.095")
        )
        is None
    )


@pytest.mark.parametrize("value", [*BAD_NUMBERS, D("0"), D("-1")])
def test_reject_invalid_paid_cost(value):
    long, short = pair()
    if value is None:
        assert spread_economics(long, short, D("0.01"), PROTOCOL, paid_long=value) is not None
    else:
        assert spread_economics(long, short, D("0.01"), PROTOCOL, paid_long=value) is None


@pytest.mark.parametrize("qty", [*BAD_NUMBERS, D("0"), D("-0.01"), D("0.015"), 0.01])
def test_reject_invalid_quantity(qty):
    long, short = pair()
    assert executable_price(long, "Buy", qty, PROTOCOL) is None
    assert spread_economics(long, short, qty, PROTOCOL) is None
    put_long, put_short = pair("Put")
    assert not bear_contract_ok(put_long, put_short, qty, PROTOCOL)


@pytest.mark.parametrize("direction", ["Call", "Put"])
@pytest.mark.parametrize(
    "overrides",
    [
        {"base": "ETH"},
        {"base": ""},
        {"expiry_ms": NOW + 22 * DAY_MS},
        {"quote_coin": "USDC"},
        {"settle_coin": "BTC"},
        {"option_type": "Other"},
        {"strike": D("0")},
    ],
)
def test_reject_mismatched_contracts(direction, overrides):
    long, short = pair(direction)
    for legs in [(replace(long, **overrides), short), (long, replace(short, **overrides))]:
        assert not structure_ok(*legs, NOW, PROTOCOL, direction=direction)
        assert spread_economics(*legs, D("0.01"), PROTOCOL, direction=direction) is None
        assert choose(legs, direction=direction) is None
        if direction == "Put":
            assert not bear_contract_ok(*legs, D("0.01"), PROTOCOL)


@pytest.mark.parametrize("direction", ["Call", "Put"])
def test_reject_wrong_type_strike_order_and_delta_sign(direction):
    long, short = pair(direction)
    assert not structure_ok(short, long, NOW, PROTOCOL, direction=direction)
    assert not structure_ok(
        long, replace(short, strike=long.strike), NOW, PROTOCOL, direction=direction
    )
    assert spread_economics(short, long, D("0.01"), PROTOCOL, direction=direction) is None
    other_type = "Put" if direction == "Call" else "Call"
    assert not structure_ok(
        replace(long, option_type=other_type), short, NOW, PROTOCOL, direction=direction
    )
    assert not structure_ok(
        replace(long, delta=-long.delta), short, NOW, PROTOCOL, direction=direction
    )
    assert not structure_ok(
        long, replace(short, delta=-short.delta), NOW, PROTOCOL, direction=direction
    )
    assert structure_ok(
        replace(long, delta=None),
        replace(short, delta=D("NaN")),
        NOW,
        PROTOCOL,
        check_deltas=False,
        direction=direction,
    )


@pytest.mark.parametrize("field", ["strike", "index_price", "delta", "expiry_ms"])
@pytest.mark.parametrize("bad", BAD_NUMBERS)
def test_structure_rejects_malformed_numbers_before_comparing(field, bad):
    long, short = pair()
    assert not structure_ok(replace(long, **{field: bad}), short, NOW, PROTOCOL)
    assert not structure_ok(long, replace(short, **{field: bad}), NOW, PROTOCOL)


@pytest.mark.parametrize("direction", ["Call", "Put"])
@pytest.mark.parametrize("days", [10, 40])
def test_entry_window_includes_boundaries(direction, days):
    long, short = [replace(leg, expiry_ms=NOW + days * DAY_MS) for leg in pair(direction)]
    assert structure_ok(long, short, NOW, PROTOCOL, direction=direction)
    outside_now = NOW + 1 if days == 10 else NOW - 1
    assert not structure_ok(long, short, outside_now, PROTOCOL, direction=direction)


@pytest.mark.parametrize(
    "field",
    [
        "strike",
        "index_price",
        "delta",
        "expiry_ms",
        "qty_step",
        "min_qty",
        "max_qty",
        "bid",
        "ask",
        "bid_size",
        "ask_size",
        "tick_size",
        "min_price",
        "max_price",
    ],
)
@pytest.mark.parametrize("bad", BAD_NUMBERS)
def test_malformed_candidate_does_not_abort_scan(field, bad):
    long, short = pair()
    expected = choose([long, short])
    broken_long = replace(long, symbol="broken-long", **{field: bad})
    broken_short = replace(short, symbol="broken-short", **{field: bad})
    assert choose([broken_long, broken_short]) is None
    assert choose([broken_short, long, broken_long, short]) == expected


@pytest.mark.parametrize("field", ["qty_step", "min_qty", "max_qty"])
@pytest.mark.parametrize("bad", [D("0"), D("-0.01")])
def test_invalid_lot_specs_do_not_abort_scan(field, bad):
    long, short = pair()
    assert choose([replace(long, **{field: bad}), short]) is None


@pytest.mark.parametrize("available", [*BAD_NUMBERS, D("0"), D("-1"), D("6.29")])
def test_reject_unusable_available_budget(available):
    assert choose(pair(), available=available) is None


@pytest.mark.parametrize("direction", ["Call", "Put"])
def test_common_step_and_first_leg_outlay_cap(direction):
    long, short = pair(direction)
    long = replace(long, qty_step=D("0.02"), min_qty=D("0.025"))
    short = replace(short, qty_step=D("0.03"))
    assert choose([long, short], direction=direction) is None  # common minimum .06 > budget
    selected = choose([long, short], direction=direction, available=D("100"))
    assert selected is not None
    assert selected.qty == D("0.06")  # LCM .06, then capped by first leg outlay
    assert selected.economics.long_cost == D("90.57")
    assert selected.reserved_risk == D("37.80")


@pytest.mark.parametrize("direction", ["Call", "Put"])
@pytest.mark.parametrize("field", ["bid_size", "ask_size", "max_qty"])
def test_sizing_respects_both_sides_depth_and_quantity_cap(direction, field):
    long, short = pair(direction)
    for legs in [
        (replace(long, **{field: D("0.025")}), short),
        (long, replace(short, **{field: D("0.025")})),
    ]:
        selected = choose(legs, direction=direction)
        assert selected is not None
        assert selected.qty == D("0.02")


@pytest.mark.parametrize("direction", ["Call", "Put"])
def test_candidate_selection_is_stable_under_permutation(direction):
    long, short = pair(direction)
    # Expiry ties use signed delta magnitude, then symbols. A signed Put comparison
    # against positive targets would incorrectly prefer the .25 short.
    sign = 1 if direction == "Call" else -1
    pool = [
        long,
        replace(long, symbol="Z-long"),
        short,
        replace(short, symbol="A-short", delta=sign * D("0.25")),
    ]
    expected = choose(pool, direction=direction)
    assert expected is not None
    assert expected.long_leg == long and expected.short_leg == short
    for ordered in permutations(pool):
        assert choose(ordered, direction=direction) == expected
    # Equally distant expiries prefer the earlier expiry, regardless of input order.
    early = [replace(leg, expiry_ms=NOW + 20 * DAY_MS) for leg in pair(direction)]
    late = [replace(leg, expiry_ms=NOW + 22 * DAY_MS) for leg in pair(direction)]
    for ordered in permutations(early + late):
        selected = choose(ordered, direction=direction)
        assert selected is not None and selected.long_leg.expiry_ms == NOW + 20 * DAY_MS


@pytest.mark.parametrize(
    ("overrides", "failed_gate"),
    [
        ({"min_reward_risk": D("1")}, "reward_risk"),
        ({"min_reward_risk": D("0.589")}, "stress_reward_risk"),
        ({"take_profit_debit_fraction": D("1")}, "take_profit_reachable"),
        ({"max_entry_friction_profit": D("0.01")}, "entry_friction"),
        ({"max_roundtrip_loss_risk": D("0.05")}, "roundtrip_risk"),
        ({"max_roundtrip_loss_profit": D("0.10")}, "roundtrip_profit"),
        ({"max_first_leg_outlay_usdt": D("15")}, "first_leg_outlay"),
    ],
)
def test_economic_gate_rejections(overrides, failed_gate):
    long, short = pair()
    protocol = replace(PROTOCOL, **overrides)
    result = spread_economics(long, short, D("0.01"), protocol)
    assert result is not None and not result.eligible
    assert failed_gate in result.failed_gates
    assert choose([long, short], protocol=protocol) is None


@pytest.mark.parametrize("paid", [D("8"), D("18.805"), D("18.80")])
def test_reject_credit_no_profit_and_stress_no_profit(paid):
    assert spread_economics(*pair(), D("0.01"), PROTOCOL, paid_long=paid) is None


def test_executable_prices_and_entry_limit_rounding():
    long, _ = pair()
    assert executable_price(long, "Buy", D("0.01"), PROTOCOL) == D("1500.5")
    assert executable_price(long, "Sell", D("0.01"), PROTOCOL, ticks=2) == D("1489")
    assert executable_price(long, "Other", D("0.01"), PROTOCOL) is None
    assert executable_price(replace(long, ask=D("1500.1")), "Buy", D("0.01"), PROTOCOL) is None
    assert executable_price(replace(long, bid=D("1501")), "Buy", D("0.01"), PROTOCOL) is None
    assert limit_price(replace(long, ask=D("1500.5")), "Buy", PROTOCOL) == D("1531")
    assert limit_price(replace(long, bid=D("890.5")), "Sell", PROTOCOL) == D("872.5")
    assert remaining_risk_budget({"BTC": D("10"), "ETH": D("5")}, PROTOCOL) == D("10")
    assert remaining_risk_budget({"BTC": D("30")}, PROTOCOL, excluding="BTC") == D("25")


@pytest.mark.parametrize("bad", [*BAD_NUMBERS, "", True])
def test_mapping_rejects_invalid_required_numbers(bad):
    data = asdict(pair()[0])
    data["ask"] = bad
    with pytest.raises(ValueError):
        SpreadLeg.from_mapping(data)
    data = asdict(pair()[0])
    data["expiry_ms"] = bad
    with pytest.raises(ValueError):
        SpreadLeg.from_mapping(data)


def test_mapping_integer_milliseconds_and_currency_metadata():
    long, short = pair()
    data = asdict(long)
    data["expiry_ms"] = "1761814400000.5"
    with pytest.raises(ValueError, match="整数毫秒"):
        SpreadLeg.from_mapping(data)
    data = asdict(long)
    del data["option_type"]
    data["optionsType"] = "Call"
    data["quoteCoin"] = "USDC"
    mapped = SpreadLeg.from_mapping(data)
    assert mapped.option_type == "Call" and mapped.quote_coin == "USDC"
    assert not structure_ok(mapped, short, NOW, PROTOCOL)
    data["quoteCoin"] = None
    assert SpreadLeg.from_mapping(data).quote_coin == ""
    for name in ("quoteCoin", "quote_coin", "settle_coin"):
        data.pop(name, None)
    mapped = SpreadLeg.from_mapping(data)
    assert mapped.quote_coin == mapped.settle_coin == "USDT"
    data["delta"] = "NaN"
    data["source_ms"] = "Infinity"
    mapped = SpreadLeg.from_mapping(data)
    assert mapped.delta is None and mapped.source_ms is None
    assert choose([mapped, short]) is None


def test_bear_contract_validation_is_independent_of_entry_window_and_greeks():
    long, short = [replace(leg, expiry_ms=NOW + DAY_MS, delta=None) for leg in pair("Put")]
    assert bear_contract_ok(long, short, D("0.01"), PROTOCOL)
    assert not bear_contract_ok(long, short, D("2"), PROTOCOL)
    assert not bear_contract_ok(replace(long, status=None), short, D("0.01"), PROTOCOL)
    assert not bear_contract_ok(replace(long, quote_coin="USDC"), short, D("0.01"), PROTOCOL)
    assert not bear_contract_ok(short, long, D("0.01"), PROTOCOL)


@pytest.mark.parametrize("direction", ["Call", "Put"])
def test_premium_fee_cap_arithmetic(direction):
    long, short = pair(direction)
    long = replace(long, bid=D("100"), ask=D("101"))
    short = replace(short, bid=D("60"), ask=D("61"))
    result = spread_economics(long, short, D("0.01"), PROTOCOL, direction=direction)
    assert result is not None
    assert result.long_cost == D("1.08605")
    assert result.debit == D("0.53270")
    assert result.profit == D("9.46730")
    assert result.stress_debit == D("0.54270")
    assert result.closing_cash == D("0.26730")
    assert result.roundtrip == D("0.26540")
    assert "roundtrip_risk" in result.failed_gates


@pytest.mark.parametrize("direction", ["Other", "call", None])
def test_reject_unknown_direction(direction):
    long, short = pair()
    assert not structure_ok(long, short, NOW, PROTOCOL, direction=direction)
    assert spread_economics(long, short, D("0.01"), PROTOCOL, direction=direction) is None
    assert choose([long, short], direction=direction) is None


def test_original_bull_pure_functions_match_valid_fixtures():
    path = Path(__file__).resolve().parents[1] / (
        "research/2026-11-15-bull-call-forward/forward.py"
    )
    spec = importlib.util.spec_from_file_location("original_bull_pure", path)
    engine = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(engine)  # definitions only; never call main/adapter/watch
    protocol = asdict(PROTOCOL)
    aliases = {
        "expiry_ms": "expiryMs",
        "index_price": "index",
        "bid_size": "bidSize",
        "ask_size": "askSize",
        "tick_size": "tick",
        "qty_step": "step",
        "min_qty": "minQty",
        "max_qty": "maxQty",
        "min_price": "minPrice",
        "max_price": "maxPrice",
    }
    legs = pair()
    originals = [{aliases.get(k, k): v for k, v in asdict(leg).items()} for leg in legs]
    for qty in [D("0.01"), D("0.03")]:
        for leg, original in zip(legs, originals, strict=True):
            for side in ["Buy", "Sell"]:
                assert executable_price(leg, side, qty, PROTOCOL, entry=True) == engine.executable(
                    original, side, qty, protocol, entry=True
                )
                assert limit_price(leg, side, PROTOCOL) == engine.limit_price(
                    original, side, protocol
                )
        assert structure_ok(*legs, NOW, PROTOCOL) == engine.structure(*originals, NOW, protocol)
        for paid in [None, D("1509.5") * qty]:
            actual = spread_economics(*legs, qty, PROTOCOL, paid_long=paid)
            expected = engine.economics(*originals, qty, protocol, paid_long=paid)
            assert actual is not None and expected is not None
            assert (
                actual.debit,
                actual.profit,
                actual.stress_debit,
                actual.long_cost,
                actual.eligible,
            ) == (
                expected["debit"],
                expected["profit"],
                expected["stressDebit"],
                expected["longCost"],
                expected["eligible"],
            )
            assert list(actual.gates.values()) == list(expected["gates"].values())
    market = {"ms": NOW, "options": {leg["symbol"]: leg for leg in originals}}
    expected = engine.choose("BTC", market, D("25"), protocol)
    selected = choose(legs)
    assert selected is not None and expected is not None
    assert (
        selected.long_leg.symbol,
        selected.short_leg.symbol,
        selected.qty,
        selected.reserved_risk,
    ) == (expected[1]["symbol"], expected[2]["symbol"], expected[3], expected[4]["stressDebit"])
