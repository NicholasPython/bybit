"""手续费口径回归（CR-02）。

Bybit 期权非 VIP taker 公式：
``fee = min(标的指数价 × 0.03%, 权利金 × 7%) × 数量``。
模拟撮合、仓位测算、预算校验与止盈判断必须共用同一口径。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal as D

import pytest

from bybitbot.adapters.base import InstrumentSpec, OrderRequest, Ticker
from bybitbot.adapters.paper import FeeModelMismatch, PaperAdapter
from bybitbot.fees import FeeModel
from bybitbot.market import QuoteCache
from bybitbot.service import DecisionView
from bybitbot.settings.models import OrderSide, OrderType
from bybitbot.strategy.base import SignalContext
from bybitbot.strategy.templates import OptionBuyParams, ScheduledOptionBuyStrategy


def model(**overrides) -> FeeModel:
    values = {"version": "test-v1", "spot_taker_rate": D("0.001")}
    values.update(overrides)
    return FeeModel(**values)


def test_option_fee_matches_official_formula():
    fees = model()
    # 指数价部分与封顶部分取小者：100000 × 0.03% = 30，权利金 100 × 7% = 7。
    assert fees.option_fee(option_price=D("100"), qty=D("1"), index_price=D("100000")) == D("7")
    # 权利金足够高时由封顶决定：1000 × 7% = 70 > 30，取指数价部分。
    assert fees.option_fee(option_price=D("1000"), qty=D("1"), index_price=D("100000")) == D("30")
    # 数量线性放大。
    assert fees.option_fee(option_price=D("1000"), qty=D("3"), index_price=D("100000")) == D("90")


def test_missing_index_price_uses_conservative_cap():
    fees = model()
    assert fees.option_fee(option_price=D("100"), qty=D("1")) == D("7")
    assert fees.option_fee(option_price=D("100"), qty=D("1"), index_price=D("0")) == D("7")
    ratio = fees.option_fee_ratio(option_price=D("100"))
    assert ratio == D("0.07")


def test_fee_ratio_matches_absolute_fee():
    fees = model()
    for price, index in ((D("100"), D("100000")), (D("1000"), D("100000")), (D("50"), None)):
        ratio = fees.option_fee_ratio(option_price=price, index_price=index)
        assert ratio * price == fees.option_fee(option_price=price, qty=D("1"), index_price=index)


def test_extra_per_contract_fee_is_included_in_budget_ratio():
    fees = model(option_per_contract=D("0.2"))
    ratio = fees.option_fee_ratio(option_price=D("100"), index_price=D("100000"))
    assert ratio == D("0.072")
    assert fees.option_fee(option_price=D("100"), qty=D("3")) == D("21.6")


@pytest.mark.parametrize("index", [D("NaN"), D("Infinity"), D("-1")])
def test_invalid_index_uses_premium_cap(index):
    assert model().option_fee(option_price=D("100"), qty=D("1"), index_price=index) == D("7")


def test_invalid_model_is_rejected():
    with pytest.raises(ValueError):
        model(version="")
    with pytest.raises(ValueError):
        model(option_premium_cap_rate=D("-0.1"))
    with pytest.raises(ValueError):
        FeeModel(version="v", spot_taker_rate=D("0.001"), option_underlying_rate=D("nan"))


@pytest.mark.parametrize("index,expected", [(None, D("0.934")), (D("10000"), D("0.970"))])
def test_option_template_sizes_quantity_with_shared_fee(index, expected):
    moment = dt.datetime(2026, 11, 1, 8, tzinfo=dt.UTC)
    symbol = "BTC-8NOV26-100000-C-USDT"
    instrument = InstrumentSpec(
        "option", symbol, "BTC", "USDT", "Trading", settle_coin="USDT",
        tick_size=D("0.1"), qty_step=D("0.001"), options_type="Call",
        strike_price=D("100000"), delivery_time=moment + dt.timedelta(days=7),
    )
    quotes = QuoteCache()
    quotes.put_ticker(Ticker(
        "option", symbol, bid_price=D("99"), ask_price=D("100"), ask_size=D("10"),
        mark_price=D("100"), delta=D("0.5"), iv=D("0.6"), index_price=index,
        source_ts=moment, received_at=moment,
    ))
    fees = model()
    strategy = ScheduledOptionBuyStrategy("fee-sizing", OptionBuyParams(
        underlying="BTC", enabled=True, premium_budget=D("100"), fee_model=fees,
    ))
    context = SignalContext("paper", "synthetic", 1, 1, "fee-sizing", moment)
    (draft,) = strategy.evaluate(
        context, DecisionView({("option", symbol): instrument}, quotes, moment),
    )
    assert draft.qty == expected
    def cost(qty):
        return D("100") * qty + fees.option_fee(
            option_price=D("100"), qty=qty, index_price=index,
        )

    assert cost(draft.qty) <= draft.spend_limit
    assert cost(draft.qty + instrument.qty_step) > draft.spend_limit
    assert draft.rationale["fee_version"] == fees.version


async def test_paper_option_fill_uses_index_price_fee():
    """模拟撮合按官方公式计费；不再按权利金的固定比例低估成本。"""
    moment = dt.datetime(2026, 11, 1, 8, tzinfo=dt.UTC)
    adapter = PaperAdapter(initial_balances={"USDT": D("1000")}, clock=lambda: moment)
    symbol = "BTC-7NOV26-100-C"
    adapter.set_instrument(
        InstrumentSpec(
            "option",
            symbol,
            "BTC",
            "USDT",
            "Trading",
            settle_coin="USDT",
            tick_size=D("0.1"),
            qty_step=D("0.1"),
        )
    )
    adapter.set_ticker(
        Ticker(
            "option",
            symbol,
            bid_price=D("500"),
            ask_price=D("501"),
            index_price=D("100000"),
            source_ts=moment,
            received_at=moment,
        )
    )
    order = adapter._register(
        OrderRequest(
            category="option",
            symbol=symbol,
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            qty=D("1"),
            price=D("500"),
            order_link_id="fee-example",
        )
    )
    adapter._apply_fill(order, D("500"), D("1"))
    # min(100000 × 0.03%, 500 × 7%) = min(30, 35) = 30 USDT。
    assert order.executions[0].exec_fee == D("30")
    assert (await adapter.get_account()).coin("USDT").available == D("470")


async def test_paper_stale_index_uses_cap_at_fill():
    moment = dt.datetime(2026, 11, 1, 8, tzinfo=dt.UTC)
    adapter = PaperAdapter(initial_balances={"USDT": D("1000")}, clock=lambda: moment)
    symbol = "BTC-7NOV26-100-C"
    adapter.set_instrument(InstrumentSpec(
        "option", symbol, "BTC", "USDT", "Trading", settle_coin="USDT",
    ))
    adapter.set_ticker(Ticker(
        "option", symbol, index_price=D("100"),
        source_ts=moment - dt.timedelta(seconds=16), received_at=moment,
    ))
    order = adapter._register(OrderRequest(
        category="option", symbol=symbol, side=OrderSide.BUY, order_type=OrderType.LIMIT,
        qty=D("1"), price=D("500"), order_link_id="stale-index",
    ))
    adapter._apply_fill(order, D("500"), D("1"))
    assert order.executions[0].exec_fee == D("35")
    assert (await adapter.get_account()).coin("USDT").available == D("465")


async def test_paper_checkpoint_fee_mismatch_is_audited():
    moment = dt.datetime(2026, 11, 1, 8, tzinfo=dt.UTC)
    adapter = PaperAdapter(initial_balances={"USDT": D("1000")}, clock=lambda: moment)
    state = adapter.export_state()
    changed = dict(state)
    changed["fees"] = dict(state["fees"])
    changed["fees"]["version"] = "other-version"
    with pytest.raises(FeeModelMismatch):
        adapter.restore_state(changed)
    legacy = dict(state)
    legacy["fees"] = {
        "version": "paper-v1-conservative",
        "spot_taker_rate": "0.001",
        "option_taker_rate": "0.0015",
    }
    with pytest.raises(FeeModelMismatch):
        adapter.restore_state(legacy)
    # 同一口径的检查点可以照常恢复。
    adapter.restore_state(state)
