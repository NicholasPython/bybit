"""公共层回归（抽公共层，第 1 步）。

这里锁住三件事：

1. **口径只有一份** —— 控制台与研究侧共用的符号解释、过滤条件提取、费用
   公式各自只有一处实现；
2. **与研究侧历史公式等价** —— 研究脚本原先各自抄了一份费用与步长公式，
   公共层必须给出同样的数，否则历史结论不可比；
3. **严格档会拒绝矛盾数据** —— 研究侧要求字段齐全且互相印证，缺一个就
   拒绝该合约，而不是猜一个值。
"""

from __future__ import annotations

import datetime as dt
import math
from decimal import Decimal as D

import pytest

from bybitbot.fees import FeeModel
from bybitbot.money import common_step
from bybitbot.terms import (
    DELIVERY_HOUR_UTC,
    OptionSpec,
    ValuationPolicy,
    delivery_fee,
    option_fee,
    option_filters,
    option_spec,
    option_symbol,
    option_value,
    parse_option_symbol,
    require_option_symbol,
    spot_fee,
)

# ---------------------------------------------------------------------------
# 研究侧公式的参考副本（保持与被迁移脚本一致，便于对照）
# ---------------------------------------------------------------------------


#: 研究/2026-11-15-bull-call-forward/forward.py:164
def research_fee(option_index, price, quantity, taker_rate=D("0.0003"), cap=D("0.07")):
    return min(taker_rate * option_index, cap * price) * quantity


#: 研究/2026-11-15-call-spread-comparison/analyze_public.py:42
def research_delivery_fee(price, strike, rate, quantity, cap=D("0.125")):
    return min(rate * price, cap * max(price - strike, D(0))) * quantity


#: 研究/2026-11-15-bull-call-forward/forward.py:307
def research_common_step(a: D, b: D) -> D:
    places = max(0, -a.as_tuple().exponent, -b.as_tuple().exponent)
    scale = 10**places
    return D(math.lcm(int(a * scale), int(b * scale))) / scale


# ---------------------------------------------------------------------------
# 符号解释
# ---------------------------------------------------------------------------


def test_parse_option_symbol_reads_strike_type_and_delivery_moment():
    terms = parse_option_symbol("BTC-27NOV26-65000-C-USDT")
    assert terms is not None
    assert terms.base == "BTC"
    assert terms.option_type == "Call"
    assert terms.strike == D("65000")
    # 到期时刻固定为交割日 08:00 UTC。
    assert terms.expiry == dt.datetime(2026, 11, 27, DELIVERY_HOUR_UTC, tzinfo=dt.UTC)
    assert terms.expiry_ms == int(terms.expiry.timestamp()) * 1000


def test_parse_option_symbol_handles_put_fractional_strike_and_missing_settle():
    terms = parse_option_symbol("ETH-30OCT26-3000.5-P")
    assert terms is not None
    assert terms.option_type == "Put"
    assert terms.strike == D("3000.5")
    assert terms.expiry == dt.datetime(2026, 10, 30, DELIVERY_HOUR_UTC, tzinfo=dt.UTC)


def test_parse_option_symbol_accepts_numeric_base_coin():
    """1000PEPE 这类含数字的标的也要能解析，否则会被误拒。"""
    terms = parse_option_symbol("1000PEPE-27NOV26-0.0075-C-USDT")
    assert terms is not None
    assert terms.base == "1000PEPE"
    assert terms.strike == D("0.0075")


@pytest.mark.parametrize(
    "symbol",
    [
        "",
        "BTCUSDT",
        "BTC-27NOV26-65000",
        "BTC-27XXX26-65000-C-USDT",
        "BTC-27NOV26-65000-X-USDT",
        "BTC-27NOV26--65000-C-USDT",
        "BTC-27NOV26-0-C-USDT",
        "btc-27nov26-65000-C-USDT",
    ],
)
def test_parse_option_symbol_returns_none_for_non_option_symbols(symbol):
    assert parse_option_symbol(symbol) is None


def test_require_option_symbol_raises_for_malformed_symbol():
    with pytest.raises(ValueError, match="不是合法的期权合约符号"):
        require_option_symbol("BTCUSDT")


def test_option_symbol_round_trips_through_parser():
    expiry = dt.datetime(2026, 11, 27, DELIVERY_HOUR_UTC, tzinfo=dt.UTC)
    symbol = option_symbol(
        base="BTC", expiry=expiry, strike=D("65000"), option_type="Call", settle="USDT"
    )
    assert symbol == "BTC-27NOV26-65000-C-USDT"
    terms = require_option_symbol(symbol)
    assert terms.strike == D("65000")
    assert terms.expiry == expiry


def test_option_symbol_does_not_scientific_notation():
    """行权价 65000 不能写成 6.5E+4 —— 交易所不接受这种符号。"""
    expiry = dt.datetime(2026, 11, 27, DELIVERY_HOUR_UTC, tzinfo=dt.UTC)
    symbol = option_symbol(
        base="BTC", expiry=expiry, strike=D("65000"), option_type="Call", settle="USDT"
    )
    assert "E+" not in symbol


# ---------------------------------------------------------------------------
# 过滤条件与合约规格
# ---------------------------------------------------------------------------


def price_filter(**overrides):
    payload = {"tickSize": "0.01", "minPrice": "0.01", "maxPrice": "9999"}
    payload.update(overrides)
    return payload


def lot_filter(**overrides):
    payload = {"qtyStep": "0.01", "minOrderQty": "0.01", "maxOrderQty": "100"}
    payload.update(overrides)
    return payload


def test_option_filters_extracts_standard_fields():
    filters = option_filters(price_filter(), lot_filter())
    assert filters.tick == D("0.01")
    assert filters.min_price == D("0.01")
    assert filters.max_price == D("9999")
    assert filters.min_qty == D("0.01")
    assert filters.step == D("0.01")
    assert filters.max_qty == D("100")


def test_option_filters_prefers_limit_order_maximum():
    """口径固定：取 maxLimitOrderQty（限价单上限），缺失时退回 maxOrderQty。"""
    filters = option_filters(price_filter(), lot_filter(maxLimitOrderQty="50", maxOrderQty="100"))
    assert filters.max_qty == D("50")
    fallback = option_filters(price_filter(), lot_filter(maxOrderQty="100"))
    assert fallback.max_qty == D("100")


def test_option_filters_tolerates_missing_and_invalid_fields():
    filters = option_filters(None, None)
    assert filters.tick is None
    assert filters.max_qty is None
    broken = option_filters(price_filter(tickSize="abc"), lot_filter(maxOrderQty=""))
    assert broken.tick is None
    assert broken.max_qty is None


def option_payload(**overrides):
    payload = {
        "symbol": "BTC-27NOV26-65000-C-USDT",
        "baseCoin": "BTC",
        "quoteCoin": "USDT",
        "settleCoin": "USDT",
        "status": "Trading",
        "optionsType": "Call",
        "deliveryTime": str(int(dt.datetime(2026, 11, 27, 8, tzinfo=dt.UTC).timestamp() * 1000)),
        "strikePrice": "65000",
        "priceFilter": price_filter(),
        "lotSizeFilter": lot_filter(),
    }
    payload.update(overrides)
    return payload


def test_option_spec_cross_validates_symbol_against_metadata():
    spec = option_spec(option_payload(), base="BTC")
    assert isinstance(spec, OptionSpec)
    assert spec.symbol == "BTC-27NOV26-65000-C-USDT"
    assert spec.strike == D("65000")
    assert spec.expiry_ms == int(dt.datetime(2026, 11, 27, 8, tzinfo=dt.UTC).timestamp() * 1000)
    assert spec.tick == D("0.01")
    assert spec.max_qty == D("100")


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"optionsType": "Put"}, "合约类型不是 Call"),
        ({"baseCoin": "ETH"}, "标的不属于 BTC"),
        ({"quoteCoin": "BTC"}, "计价币不是 USDT"),
        ({"deliveryTime": "0"}, "到期时刻与 deliveryTime 不一致"),
        ({"strikePrice": "64000"}, "行权价与 strikePrice 不一致"),
        ({"strikePrice": "NaN"}, "行权价与 strikePrice 不一致"),
        ({"strikePrice": ""}, "行权价与 strikePrice 不一致"),
        ({"symbol": "BTC-27NOV26-65000-C-USDC"}, "结算币与 settleCoin 不一致"),
        ({"deliveryTime": "Infinity"}, "不是整数毫秒"),
        ({"status": ""}, "合约状态缺失"),
        ({"priceFilter": {"minPrice": "0.01", "maxPrice": "9999"}}, "过滤条件缺失"),
        ({"lotSizeFilter": {"qtyStep": "0.01", "minOrderQty": "0.01"}}, "过滤条件缺失"),
    ],
)
def test_option_spec_rejects_inconsistent_entries(overrides, message):
    with pytest.raises(ValueError, match=message):
        option_spec(option_payload(**overrides), base="BTC")


def test_option_spec_rejects_impossible_filter_ranges():
    with pytest.raises(ValueError, match="数量区间不合法"):
        option_spec(
            option_payload(lotSizeFilter=lot_filter(minOrderQty="200", maxOrderQty="100")),
            base="BTC",
        )
    with pytest.raises(ValueError, match="价格区间不合法"):
        option_spec(
            option_payload(priceFilter=price_filter(minPrice="2", maxPrice="1")), base="BTC"
        )
    with pytest.raises(ValueError, match="过滤条件必须为正"):
        option_spec(option_payload(lotSizeFilter=lot_filter(qtyStep="0")), base="BTC")


# ---------------------------------------------------------------------------
# 费用口径
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("index", "premium", "qty"),
    [
        (D("100000"), D("100"), D("1")),
        (D("100000"), D("1000"), D("3")),
        (D("95000"), D("1234.5"), D("2")),
        (D("42"), D("0.05"), D("10")),
    ],
)
def test_option_fee_matches_research_formula(index, premium, qty):
    assert option_fee(
        premium=premium,
        qty=qty,
        index_price=index,
        underlying_rate=D("0.0003"),
        premium_cap_rate=D("0.07"),
    ) == research_fee(index, premium, qty)


def test_option_fee_without_index_price_falls_back_to_premium_cap():
    """拿不到指数价时按封顶计费 —— 保守方向，不高估收益。"""
    premium, qty = D("100"), D("2")
    expected = premium * D("0.07") * qty
    assert (
        option_fee(
            premium=premium,
            qty=qty,
            index_price=None,
            underlying_rate=D("0.0003"),
            premium_cap_rate=D("0.07"),
        )
        == expected
    )
    # 非正数的指数价等同于拿不到。
    assert (
        option_fee(
            premium=premium,
            qty=qty,
            index_price=D("0"),
            underlying_rate=D("0.0003"),
            premium_cap_rate=D("0.07"),
        )
        == expected
    )


def test_option_fee_rejects_negative_inputs():
    with pytest.raises(ValueError, match="期权价格"):
        option_fee(
            premium=D("-1"),
            qty=D("1"),
            index_price=D("100"),
            underlying_rate=D("0.0003"),
            premium_cap_rate=D("0.07"),
        )
    with pytest.raises(ValueError, match="数量"):
        option_fee(
            premium=D("1"),
            qty=D("-1"),
            index_price=D("100"),
            underlying_rate=D("0.0003"),
            premium_cap_rate=D("0.07"),
        )


def test_console_fee_model_delegates_to_shared_layer():
    """控制台的费用模型与公共层必须给出同一个数。"""
    model = FeeModel(version="test-v1", spot_taker_rate=D("0.001"))
    assert model.option_fee(option_price=D("1000"), qty=D("3"), index_price=D("100000")) == D("90")
    assert model.option_fee(option_price=D("1000"), qty=D("3"), index_price=None) == D("210")
    assert model.spot_fee(price=D("100"), qty=D("2")) == D("0.200")
    assert model.option_fee_ratio(option_price=D("1000"), index_price=D("100000")) == D("0.03")


def test_spot_fee_matches_amount_times_rate():
    assert spot_fee(price=D("100"), qty=D("2"), rate=D("0.001")) == D("0.200")


@pytest.mark.parametrize(
    ("price", "strike", "rate", "qty"),
    [
        (D("100"), D("90"), D("0.00055"), D("1")),
        (D("120"), D("100"), D("0.00055"), D("3")),
        (D("50"), D("100"), D("0.00055"), D("2")),
        (D("100"), D("100"), D("0.00055"), D("1")),
    ],
)
def test_delivery_fee_matches_research_formula(price, strike, rate, qty):
    assert delivery_fee(
        price=price, strike=strike, rate=rate, cap_rate=D("0.125"), qty=qty
    ) == research_delivery_fee(price, strike, rate, qty)


# ---------------------------------------------------------------------------
# 数量步长
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("a", "b"),
    [
        (D("0.05"), D("0.02")),
        (D("1"), D("0.5")),
        (D("0.01"), D("0.01")),
        (D("5"), D("1")),
        (D("0.0001"), D("0.001")),
    ],
)
def test_common_step_matches_research_lcm(a, b):
    assert common_step(a, b) == research_common_step(a, b)


@pytest.mark.parametrize("invalid", ["0", "-1", "NaN", "Infinity"])
def test_common_step_rejects_non_positive_steps(invalid):
    with pytest.raises(ValueError, match="步长"):
        common_step(D(invalid), D("1"))


def test_common_step_finds_smallest_shared_quantity_grid():
    assert common_step(D("0.05"), D("0.02")) == D("0.1")
    assert common_step(D("0.3"), D("0.2")) == D("0.6")


def test_valuation_policy_is_the_same_public_contract():
    from bybitbot.valuation import ValuationPolicy as ConsolePolicy

    assert ConsolePolicy is ValuationPolicy
    assert option_value(price=D("50"), qty=D("2"), multiplier=D("0.1"), rate=D("0.9")) == D("9")


@pytest.mark.parametrize(
    "overrides",
    [
        {"price": D("NaN")},
        {"price": D("-1")},
        {"qty": D("Infinity")},
        {"multiplier": D("0")},
        {"rate": D("-1")},
    ],
)
def test_option_value_rejects_invalid_valuation_factors(overrides):
    with pytest.raises(ValueError):
        option_value(**({"price": D("50"), "qty": D("2")} | overrides))


@pytest.mark.parametrize("value", ["NaN", "Infinity", "abc", True, None])
def test_filters_reject_nonfinite_or_malformed_numbers(value):
    assert option_filters(price_filter(tickSize=value), None).tick is None
    with pytest.raises(ValueError, match="过滤条件缺失"):
        option_spec(option_payload(priceFilter=price_filter(tickSize=value)), base="BTC")


@pytest.mark.parametrize(
    "overrides",
    [
        {"strike": D("NaN")},
        {"base": "B-TC"},
        {"settle": ""},
        {"expiry": dt.datetime(2026, 11, 27, 8)},
        {"expiry": dt.datetime(2026, 11, 27, 0, tzinfo=dt.UTC)},
        {"expiry": dt.datetime(2100, 11, 27, 8, tzinfo=dt.UTC)},
    ],
)
def test_option_symbol_rejects_terms_that_cannot_round_trip(overrides):
    values = {
        "base": "BTC",
        "strike": D("65000"),
        "option_type": "Call",
        "expiry": dt.datetime(2026, 11, 27, 8, tzinfo=dt.UTC),
    }
    with pytest.raises(ValueError):
        option_symbol(**(values | overrides))
