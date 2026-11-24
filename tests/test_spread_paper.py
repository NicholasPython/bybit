from __future__ import annotations

import datetime as dt
import json
from dataclasses import replace
from decimal import Decimal as D

import pytest

from bybitbot.adapters.base import (
    InstrumentSpec,
    OrderBook,
    OrderRejected,
    OrderRequest,
    OrderUncertain,
    Ticker,
)
from bybitbot.adapters.paper import FeeModelMismatch, PaperAdapter
from bybitbot.fees import FeeModel
from bybitbot.settings.models import OrderSide, OrderType, TimeInForce
from bybitbot.spread.paper import LINEAR_TAKER_RATE, LinearPaperAdapter


@pytest.fixture
def market():
    moment = [dt.datetime(2026, 11, 20, tzinfo=dt.UTC)]
    adapter = LinearPaperAdapter(initial_balances={"USDT": D("1000")}, clock=lambda: moment[0])
    for symbol in ("BTCUSDT", "ETHUSDT"):
        adapter.set_instrument(spec(symbol))
    return adapter, moment


def spec(symbol="BTCUSDT", category="linear"):
    return InstrumentSpec(
        category=category,
        symbol=symbol,
        base_coin=symbol.removesuffix("USDT"),
        quote_coin="USDT",
        settle_coin="USDT" if category != "spot" else None,
        status="Trading",
        tick_size=D("0.1"),
        qty_step=D("0.1"),
        min_order_qty=D("0.1"),
    )


def request(link="one", symbol="BTCUSDT", side="Buy", qty="1", price="100", **kwargs):
    return OrderRequest(
        category="linear",
        symbol=symbol,
        side=OrderSide(side),
        order_type=OrderType.LIMIT,
        qty=D(qty),
        price=D(price),
        order_link_id=link,
        position_idx=0,
        **kwargs,
    )


def quote(
    adapter,
    moment,
    symbol="BTCUSDT",
    bid="99",
    ask="100",
    depth="10",
    mark=None,
    category="linear",
    advance=True,
):
    if advance:
        moment[0] += dt.timedelta(seconds=1)
    if mark is not None:
        adapter.set_ticker(
            Ticker(category, symbol, mark_price=D(mark), source_ts=moment[0], received_at=moment[0])
        )
    book = OrderBook(
        category,
        symbol,
        bids=((D(bid), D(depth)),),
        asks=((D(ask), D(depth)),),
        source_ts=moment[0],
        received_at=moment[0],
    )
    adapter.set_book(book)
    return book


async def test_two_leg_open_mark_and_close_realised_cash_pnl(market):
    adapter, moment = market
    await adapter.create_order(request("long", qty="2"))
    await adapter.create_order(request("short", "ETHUSDT", "Sell", "4", "50"))
    assert not await adapter.get_executions("linear")
    quote(adapter, moment, mark="100")
    quote(adapter, moment, "ETHUSDT", bid="50", ask="51", mark="50")
    opened = await adapter.get_account()
    assert opened.coin("USDT").wallet_balance == D("999.78")
    assert opened.coin("USDT").locked == D("400")
    assert opened.coin("USDT").available == D("599.78")
    assert opened.total_equity == opened.exchange_equity == D("999.78")
    positions = {p.symbol: p for p in await adapter.get_positions("linear")}
    assert (positions["BTCUSDT"].size, positions["BTCUSDT"].side) == (D("2"), "Buy")
    assert (positions["ETHUSDT"].size, positions["ETHUSDT"].side) == (D("4"), "Sell")

    quote(adapter, moment, bid="110", ask="111", mark="110")
    quote(adapter, moment, "ETHUSDT", bid="44", ask="45", mark="45")
    marked = await adapter.get_account()
    assert marked.coin("USDT").wallet_balance == D("999.78")
    assert marked.coin("USDT").equity == marked.total_equity == D("1039.78")
    assert D(marked.raw["linear_unrealised_pnl"]) == D("40")
    assert D(marked.raw["total_notional"]) == D("400")
    assert D(marked.raw["account_mm_rate"]) == D("2") / D("1039.78")
    assert "No exchange liquidation, funding" in marked.raw["margin_estimate"]

    await adapter.create_order(
        request("close-long", side="Sell", qty="2", price="110", reduce_only=True)
    )
    await adapter.create_order(
        request("close-short", "ETHUSDT", "Buy", "4", "45", reduce_only=True)
    )
    quote(adapter, moment, bid="110", ask="111", mark="110")
    quote(adapter, moment, "ETHUSDT", bid="44", ask="45", mark="45")
    closed = await adapter.get_account()
    assert closed.coin("USDT").wallet_balance == closed.total_equity == D("1039.56")
    assert closed.coin("USDT").available == D("1039.56")
    assert closed.coin("USDT").locked == 0
    assert all(p.size == 0 for p in await adapter.get_positions("linear"))
    fills = await adapter.get_executions("linear")
    assert sum(e.exec_fee for e in fills) == D("0.44")
    assert all(e.exec_fee_asset == "USDT" for e in fills)
    assert all(e.raw["fee_version"] == adapter.linear_fee_version for e in fills)
    assert sum(p.realised_pnl for p in await adapter.get_positions("linear")) == D("40")


@pytest.mark.parametrize("side,close_side", [("Buy", "Sell"), ("Sell", "Buy")])
async def test_reduce_only_clips_and_never_reverses(market, side, close_side):
    adapter, moment = market
    opening_price = "100" if side == "Buy" else "99"
    await adapter.create_order(request(side=side, qty="2", price=opening_price))
    quote(adapter, moment)
    with pytest.raises(OrderRejected, match="opposite"):
        await adapter.create_order(request("wrong", side=side, reduce_only=True))
    close_price = "100" if close_side == "Buy" else "99"
    await adapter.create_order(
        request("close-a", side=close_side, qty="3", price=close_price, reduce_only=True)
    )
    await adapter.create_order(
        request("close-b", side=close_side, qty="2", price=close_price, reduce_only=True)
    )
    quote(adapter, moment)
    position = (await adapter.get_positions("linear"))[0]
    assert position.size == 0 and position.side is None
    orders = {o.order_link_id: o for o in await adapter.get_order_history("linear")}
    assert orders["close-a"].qty_filled == 2
    assert orders["close-a"].status == "Cancelled"
    assert orders["close-b"].qty_filled == 0
    assert orders["close-b"].status == "Cancelled"
    assert not await adapter.get_open_orders("linear")
    with pytest.raises(OrderRejected):
        await adapter.create_order(request("flat-close", side=close_side, reduce_only=True))


async def test_one_way_average_partial_close_and_flip(market):
    adapter, moment = market
    await adapter.create_order(request("first", qty="2"))
    quote(adapter, moment, mark="100")
    await adapter.create_order(request("add", price="110"))
    quote(adapter, moment, bid="109", ask="110", mark="110")
    position = (await adapter.get_positions("linear"))[0]
    assert position.size == 3
    assert position.avg_price == D("310") / 3
    await adapter.create_order(request("flip", side="Sell", qty="4", price="120"))
    quote(adapter, moment, bid="120", ask="121", mark="120")
    position = (await adapter.get_positions("linear"))[0]
    assert (position.size, position.side, position.avg_price) == (D("1"), "Sell", D("120"))
    assert abs(position.realised_pnl - D("50")) < D("1e-24")
    fees = D("790") * LINEAR_TAKER_RATE
    assert abs((await adapter.get_account()).coin("USDT").wallet_balance - (D("1050") - fees)) < D(
        "1e-24"
    )


async def test_ioc_shares_depth_cancels_remainder_and_never_replays_book(market):
    adapter, moment = market
    await adapter.create_order(request("a", time_in_force=TimeInForce.IOC))
    await adapter.create_order(request("b", time_in_force=TimeInForce.IOC))
    book = quote(adapter, moment, depth="0.4", mark="100")
    adapter.set_book(book)
    fills = await adapter.get_executions("linear")
    assert [(e.order_link_id, e.exec_qty) for e in fills] == [("a", D("0.4"))]
    orders = await adapter.get_order_history("linear")
    assert [o.status for o in orders] == ["Cancelled", "Cancelled"]
    assert [o.qty_filled for o in orders] == [D("0.4"), D("0")]
    quote(adapter, moment, depth="10")
    assert len(await adapter.get_executions("linear")) == 1
    assert (await adapter.get_account()).coin("USDT").locked == D("40")


async def test_gtc_partial_fault_and_multiple_price_levels(market):
    adapter, moment = market
    await adapter.create_order(request(qty="2", price="102"))
    adapter.set_partial_ratio(D("0.5"))
    adapter.inject_fault("partial")
    quote(adapter, moment, depth="10", mark="100")
    assert (await adapter.get_open_orders("linear"))[0].qty_filled == 1
    moment[0] += dt.timedelta(seconds=1)
    adapter.set_book(
        OrderBook(
            "linear",
            "BTCUSDT",
            ((D("99"), D("10")),),
            ((D("101"), D("0.4")), (D("102"), D("0.6"))),
            source_ts=moment[0],
            received_at=moment[0],
        )
    )
    fills = await adapter.get_executions("linear")
    assert [(e.exec_price, e.exec_qty) for e in fills] == [
        (D("100"), D("1")),
        (D("101"), D("0.4")),
        (D("102"), D("0.6")),
    ]
    assert (await adapter.get_order_history("linear"))[0].avg_price == D("100.8")


@pytest.mark.parametrize("fault", ["after_send_timeout", "unknown_result"])
async def test_checkpoint_and_retry_are_idempotent_before_and_after_fill(market, fault):
    adapter, moment = market
    intent = request(time_in_force=TimeInForce.IOC)
    adapter.inject_fault(fault)
    with pytest.raises(OrderUncertain):
        await adapter.create_order(intent)
    state = json.loads(json.dumps(adapter.export_state()))
    restored = LinearPaperAdapter(initial_balances={"USDT": D("999999")}, clock=lambda: moment[0])
    restored.restore_state(state)
    assert restored.export_state() == state
    ack = await restored.create_order(intent)
    assert ack.order_id == state["linear"]["idempotency"][intent.order_link_id]
    quote(restored, moment, depth="0.4", mark="100")
    second_state = json.loads(json.dumps(restored.export_state()))
    restored_again = LinearPaperAdapter(clock=lambda: moment[0])
    restored_again.restore_state(second_state)
    assert restored_again.export_state() == second_state
    retry = await restored_again.create_order(intent)
    assert retry.order_id == ack.order_id and retry.status == "Cancelled"
    quote(restored_again, moment)
    assert len(await restored_again.get_executions("linear")) == 1
    assert (await restored_again.get_account()).coin("USDT").wallet_balance == D("999.978")
    with pytest.raises(OrderUncertain, match="different request"):
        await restored_again.create_order(replace(intent, qty=D("2")))


async def test_active_gtc_checkpoint_reserves_margin_and_resumes_once(market):
    adapter, moment = market
    await adapter.create_order(request(qty="2"))
    old_book = quote(adapter, moment, depth="0.4", mark="100")
    before = (await adapter.get_account()).coin("USDT").locked
    restored = LinearPaperAdapter(clock=lambda: moment[0])
    restored.restore_state(adapter.export_state())
    unquoted = await restored.get_account()
    assert unquoted.total_equity is None
    assert unquoted.exchange_equity is None
    assert unquoted.raw["account_mm_rate"] is None
    assert unquoted.raw["total_notional"] is None
    assert unquoted.raw["linear_unrealised_pnl"] is None
    assert unquoted.raw["linear_missing_quotes"] == ["BTCUSDT"]
    assert unquoted.coin("USDT").available == 0
    assert unquoted.coin("USDT").locked == before
    restored.set_book(old_book)
    assert len(await restored.get_executions("linear")) == 1
    quote(restored, moment, depth="2", mark="100")
    assert sum(e.exec_qty for e in await restored.get_executions("linear")) == 2
    assert (await restored.get_positions("linear"))[0].size == 2
    assert (await restored.get_account()).total_equity == D("999.89")


async def test_post_only_rejects_crossing_requires_quote_and_fills_as_maker(market):
    adapter, moment = market
    passive = request(time_in_force=TimeInForce.POST_ONLY)
    with pytest.raises(OrderRejected, match="fresh"):
        await adapter.create_order(passive)
    quote(adapter, moment)
    with pytest.raises(OrderRejected, match="take liquidity"):
        await adapter.create_order(passive)
    with pytest.raises(OrderRejected, match="take liquidity"):
        await adapter.create_order(replace(passive, side=OrderSide.SELL, price=D("99")))
    quote(adapter, moment, ask="101")
    await adapter.create_order(passive)
    assert not await adapter.get_executions("linear")
    quote(adapter, moment, bid="98", ask="99", mark="99")
    fill = (await adapter.get_executions("linear"))[0]
    assert fill.is_maker is True
    assert fill.exec_price == D("99")
    assert fill.exec_fee == D("99") * LINEAR_TAKER_RATE
    moment[0] += dt.timedelta(seconds=16)
    with pytest.raises(OrderRejected, match="fresh"):
        await adapter.create_order(replace(passive, order_link_id="stale"))


async def test_pending_orders_and_positions_cannot_reuse_collateral(market):
    adapter, moment = market
    await adapter.create_order(request("a", qty="6"))
    reserved = await adapter.get_account()
    assert reserved.coin("USDT").locked == D("600.33")
    with pytest.raises(OrderRejected, match="collateral"):
        await adapter.create_order(request("b", "ETHUSDT", "Sell", "5", "100"))
    await adapter.cancel_order("linear", "BTCUSDT", order_link_id="a")
    assert (await adapter.get_account()).coin("USDT").available == 1000
    await adapter.create_order(request("b", "ETHUSDT", "Sell", "5", "100"))
    quote(adapter, moment, "ETHUSDT", bid="100", ask="101", mark="100")
    with pytest.raises(OrderRejected, match="collateral"):
        await adapter.create_order(request("c", qty="5"))
    await adapter.create_order(request("close", "ETHUSDT", "Buy", "5", "101", reduce_only=True))
    quote(adapter, moment, "ETHUSDT", bid="100", ask="101", mark="100")
    assert (await adapter.get_account()).coin("USDT").locked == 0


async def test_missing_or_stale_marks_block_new_risk_but_allow_reduction(market):
    adapter, moment = market
    await adapter.create_order(request())
    quote(adapter, moment, mark="100")
    moment[0] += dt.timedelta(seconds=16)
    account = await adapter.get_account()
    assert account.total_equity is None and account.raw["linear_unrealised_pnl"] is None
    assert (await adapter.get_positions("linear"))[0].unrealised_pnl is None
    with pytest.raises(OrderRejected, match="collateral"):
        await adapter.create_order(request("new", "ETHUSDT"))
    await adapter.create_order(request("close", side="Sell", price="99", reduce_only=True))
    quote(adapter, moment, mark="99")
    assert (await adapter.get_positions("linear"))[0].size == 0
    assert (await adapter.get_account()).total_equity is not None


async def test_underwater_reduction_is_not_blocked_by_margin_or_close_fee(market):
    adapter, moment = market
    await adapter.create_order(request(side="Sell", qty="8", price="100"))
    quote(adapter, moment, bid="100", ask="101", mark="100")
    quote(adapter, moment, bid="299", ask="300", mark="300")
    account = await adapter.get_account()
    assert account.total_equity < 0
    assert account.coin("USDT").available == 0
    await adapter.create_order(request("close", qty="8", price="300", reduce_only=True))
    quote(adapter, moment, bid="299", ask="300", mark="300")
    assert (await adapter.get_positions("linear"))[0].size == 0
    assert (await adapter.get_account()).coin("USDT").wallet_balance == D("-601.76")


async def test_fill_rechecks_margin_after_price_gap_without_consuming_depth(market):
    adapter, moment = market
    await adapter.create_order(request("a", side="Sell", qty="6", price="100"))
    await adapter.create_order(request("b", side="Sell", qty="1", price="100"))
    quote(adapter, moment, bid="200", ask="201", depth="1", mark="200")
    # The first order's remaining reserve also doubles; it cannot open safely.
    history = {o.order_link_id: o for o in await adapter.get_order_history("linear")}
    assert history["a"].status == "Cancelled"
    assert history["a"].qty_filled == 0
    assert history["b"].status == "Filled"
    assert (await adapter.get_positions("linear"))[0].size == 1


async def test_spot_same_symbol_delegates_and_cannot_spend_linear_reserve(market):
    adapter, moment = market
    adapter.set_instrument(spec(category="spot"))
    await adapter.create_order(request("linear", side="Sell", qty="8", price="100"))
    spot = replace(request("spot", qty="3"), category="spot")
    with pytest.raises(OrderRejected):
        await adapter.create_order(spot)
    await adapter.create_order(replace(spot, qty=D("1")))
    quote(adapter, moment, category="spot", mark="100")
    assert len(await adapter.get_executions("spot")) == 1
    assert not await adapter.get_executions("linear")
    assert (await adapter.get_account()).coin("BTC").wallet_balance == D("0.999")
    quote(adapter, moment, bid="100", ask="101", mark="100")
    assert (await adapter.get_positions("linear"))[0].side == "Sell"
    assert (await adapter.get_positions("spot"))[0].side == "Buy"
    assert (await adapter.get_ticker("spot", "BTCUSDT")).mark_price == D("100")
    assert (await adapter.get_orderbook("linear", "BTCUSDT")).bid() == D("100")
    assert (await adapter.get_orderbook("spot", "BTCUSDT")).bid() == D("99")
    assert (await adapter.get_instruments("spot"))[0].category == "spot"
    assert (await adapter.get_instruments("linear"))[0].category == "linear"


async def test_linear_cannot_spend_reserved_spot_cash(market):
    adapter, _ = market
    adapter.set_instrument(spec(category="spot"))
    await adapter.create_order(replace(request("spot", qty="8"), category="spot"))
    with pytest.raises(OrderRejected, match="collateral"):
        await adapter.create_order(request("linear", qty="3"))


async def test_legacy_checkpoint_spot_option_restore_and_parent_fees(market):
    _, moment = market
    fees = FeeModel("test-fees", spot_taker_rate=D("0.002"))
    parent = PaperAdapter(
        initial_balances={"USDT": D("1000")}, fee_model=fees, clock=lambda: moment[0]
    )
    parent.set_instrument(spec(category="spot"))
    parent.set_instrument(spec("BTC-OPTION", category="option"))
    await parent.create_order(replace(request("spot"), category="spot"))
    await parent.create_order(replace(request("option", symbol="BTC-OPTION"), category="option"))
    state = parent.export_state()
    child = LinearPaperAdapter(fee_model=fees, clock=lambda: moment[0])
    child.restore_state(state)
    quote(child, moment, category="spot")
    quote(child, moment, symbol="BTC-OPTION", category="option")
    assert (await child.get_executions("spot"))[0].exec_fee == D("0.002")
    assert (await child.get_executions("option"))[0].exec_fee == D("7")
    assert (await child.get_account()).coin("USDT").wallet_balance == D("793")
    child.set_instrument(spec())
    await child.create_order(request("linear"))
    quote(child, moment, mark="100")
    assert (await child.get_executions("linear"))[0].exec_fee == D("0.055")


def test_linear_checkpoint_fee_version_and_idempotency_must_match(market):
    adapter, moment = market
    state = adapter.export_state()
    assert state["linear"]["taker_rate"] == "0.00055"
    different = LinearPaperAdapter(linear_taker_rate=D("0.0006"), clock=lambda: moment[0])
    with pytest.raises(FeeModelMismatch):
        different.restore_state(state)
    state["linear"]["idempotency"] = {"ghost": "paper-00000001"}
    with pytest.raises(ValueError, match="idempotency"):
        adapter.restore_state(state)


async def test_book_only_ticker_has_synthetic_mark_and_does_not_refresh_old_mark(market):
    adapter, moment = market
    quote(adapter, moment, bid="99", ask="101")
    ticker = await adapter.get_ticker("linear", "BTCUSDT")
    assert (ticker.bid_price, ticker.ask_price, ticker.mark_price) == (D("99"), D("101"), D("100"))
    assert ticker.source_ts == moment[0]
    assert (await adapter.get_account()).raw["linear_mark_sources"][
        "BTCUSDT"
    ] == "book_mid_estimate"
    adapter.set_ticker(replace(ticker, mark_price=D("150")))
    old_time = moment[0]
    quote(adapter, moment, bid="100", ask="102")
    ticker = await adapter.get_ticker("linear", "BTCUSDT")
    assert ticker.mark_price == D("150")
    assert ticker.source_ts == old_time
    moment[0] += dt.timedelta(seconds=16)
    quote(adapter, moment, bid="200", ask="202")
    ticker = await adapter.get_ticker("linear", "BTCUSDT")
    assert ticker.mark_price == D("201")
    assert ticker.source_ts == moment[0]
    moment[0] += dt.timedelta(seconds=16)
    ticker = await adapter.get_ticker("linear", "BTCUSDT")
    assert ticker.mark_price is None and ticker.bid_price is None and ticker.source_ts is None


async def test_invalid_or_same_time_book_cannot_fill_or_cancel_ioc(market):
    adapter, moment = market
    existing = quote(adapter, moment)
    await adapter.create_order(request(time_in_force=TimeInForce.IOC))
    adapter.set_book(existing)
    for invalid in (
        replace(existing, gap_detected=True),
        replace(existing, source_ts=None),
        replace(existing, asks=((D("98"), D("1")),)),
    ):
        adapter.set_book(invalid)
    assert (await adapter.get_open_orders("linear"))[0].status == "New"
    quote(adapter, moment, ask="101")
    assert not await adapter.get_executions("linear")
    assert (await adapter.get_order_history("linear"))[0].status == "Cancelled"


@pytest.mark.parametrize(
    "change",
    [
        {"position_idx": 1},
        {"order_type": OrderType.MARKET},
        {"time_in_force": TimeInForce.FOK},
        {"qty": D("0.01")},
        {"price": D("NaN")},
    ],
)
async def test_unsupported_or_invalid_requests_fail_closed(market, change):
    adapter, _ = market
    with pytest.raises(OrderRejected):
        await adapter.create_order(replace(request(), **change))
    assert not await adapter.get_order_history("linear")
