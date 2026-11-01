from __future__ import annotations

import datetime as dt
import json
from dataclasses import replace
from decimal import Decimal as D

import pytest

from bybitbot.adapters.base import InstrumentSpec, OrderBook, OrderRequest, OrderUncertain
from bybitbot.adapters.paper import PaperAdapter
from bybitbot.settings.models import OrderSide, OrderType


@pytest.fixture
def market():
    moment = [dt.datetime(2026, 10, 31, 8, tzinfo=dt.UTC)]
    adapter = PaperAdapter(initial_balances={"USDT": D("1000")}, clock=lambda: moment[0])
    adapter.set_instrument(
        InstrumentSpec(
            "spot",
            "BTCUSDT",
            "BTC",
            "USDT",
            "Trading",
            tick_size=D("0.1"),
            qty_step=D("0.001"),
            min_order_qty=D("0.001"),
        )
    )
    return adapter, moment


def request(link="one", qty="1", side=OrderSide.BUY):
    return OrderRequest(
        "spot", "BTCUSDT", side, OrderType.LIMIT, D(qty), D("100"), order_link_id=link
    )


def book(moment, depth="0.4", ask="100"):
    return OrderBook(
        "spot",
        "BTCUSDT",
        ((D("99"), D(depth)),),
        ((D(ask), D(depth)),),
        source_ts=moment,
        received_at=moment,
    )


async def test_next_book_depth_shared_cancel_and_checkpoint(market):
    adapter, moment = market
    adapter.set_book(book(moment[0]))
    await adapter.create_order(request("a"))
    await adapter.create_order(request("b"))
    assert not await adapter.get_executions("spot")
    moment[0] += dt.timedelta(seconds=1)
    adapter.set_book(book(moment[0]))
    fills = await adapter.get_executions("spot")
    assert [(e.order_link_id, e.exec_qty) for e in fills] == [("a", D("0.4"))]
    assert fills[0].exec_fee == D("0.0004")
    assert fills[0].exec_fee_asset == "BTC"
    assert (await adapter.get_account()).coin("BTC").available == D("0.3996")
    adapter.set_book(book(moment[0]))
    assert len(await adapter.get_executions("spot")) == 1
    await adapter.cancel_order("spot", "BTCUSDT", order_link_id="a")
    moment[0] += dt.timedelta(seconds=1)
    adapter.set_book(book(moment[0]))
    assert (await adapter.get_executions("spot"))[-1].order_link_id == "b"
    state = json.loads(json.dumps(adapter.export_state()))
    restored = PaperAdapter(initial_balances={"USDT": D("999999")}, clock=lambda: moment[0])
    restored.restore_state(state)
    assert restored.export_state() == state
    assert (await restored.get_account()).coin("USDT").available == D("920")
    restored.set_book(book(moment[0]))
    assert len(await restored.get_executions("spot")) == 2
    moment[0] += dt.timedelta(seconds=1)
    restored.set_book(book(moment[0], "2"))
    fills = await restored.get_executions("spot")
    assert len({e.exec_id for e in fills}) == len(fills)
    assert sum(e.exec_qty for e in fills if e.order_link_id == "a") == D("0.4")
    assert sum(e.exec_qty for e in fills if e.order_link_id == "b") == D("1")


async def test_invalid_book_and_non_touch_do_not_fill(market):
    adapter, moment = market
    await adapter.create_order(request())
    moment[0] += dt.timedelta(seconds=1)
    adapter.set_book(book(moment[0], ask="101"))
    assert not await adapter.get_executions("spot")
    adapter.set_book(replace(book(moment[0]), gap_detected=True))
    adapter.set_book(replace(book(moment[0]), asks=((D("98"), D("1")),)))
    assert not await adapter.get_executions("spot")


async def test_unknown_registered_and_survives_json_restore(market):
    adapter, moment = market
    adapter.inject_fault("unknown_result")
    with pytest.raises(OrderUncertain):
        await adapter.create_order(request())
    with pytest.raises(OrderUncertain):
        await adapter.create_order(request())
    restored = PaperAdapter(clock=lambda: moment[0])
    restored.restore_state(adapter.export_state())
    assert len(await restored.get_open_orders("spot")) == 1
    moment[0] += dt.timedelta(seconds=1)
    restored.set_book(book(moment[0], "1"))
    assert len(await restored.get_executions("spot")) == 1


async def test_option_close_credits_cash_and_cannot_short(market):
    adapter, moment = market
    symbol = "BTC-7NOV26-100-C-USDT"
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
    buy = replace(request(), category="option", symbol=symbol, qty=D("1"))
    await adapter.create_order(buy)
    moment[0] += dt.timedelta(seconds=1)
    adapter.set_book(replace(book(moment[0], "1"), category="option", symbol=symbol))
    # 期权费用按官方公式估算；没有标的指数价时按封顶比例计（保守方向）：
    # 权利金 100 × 7% = 7 USDT。
    assert (await adapter.get_account()).coin("USDT").available == D("893.00")
    close = replace(
        buy,
        side=OrderSide.SELL,
        price=D("99"),
        order_link_id="close",
        reduce_only=True,
        position_idx=0,
    )
    await adapter.create_order(close)
    moment[0] += dt.timedelta(seconds=1)
    adapter.set_book(replace(book(moment[0], "1"), category="option", symbol=symbol))
    # 卖出所得 99 - 99 × 7% = 92.07。
    assert (await adapter.get_account()).coin("USDT").available == D("985.07")
    assert (await adapter.get_positions("option"))[0].size == 0
