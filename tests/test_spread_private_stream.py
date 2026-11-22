"""私有 WebSocket 成交流 → BLSAE 归约器的隔离测试（mock/paper）。

这些测试不连真实交易所、不写数据库、不启动 worker，只验证事件适配层的
fail-closed 与幂等语义。"""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal as D
from unittest.mock import AsyncMock

import pytest

from bybitbot.adapters.base import (
    ExecutionUpdate,
    ExecutionView,
    OrderUpdate,
    OrderView,
    PositionView,
)
from bybitbot.adapters.bybit_ws import BybitPrivateStream, BybitWebSocketClient, WebSocketError
from bybitbot.spread.events import (
    CalibrationSnapshot,
    EventAdapterError,
    OrderIdentity,
    PrivateExecutionAdapter,
    reduce_private_events,
)
from bybitbot.spread.execution import (
    ACTION_SUBMIT,
    LEG_A,
    LEG_B,
    new_execution,
)

PAIR = "pt001"
A, B = "BTCUSDT", "ETHUSDT"
NOW = dt.datetime(2026, 11, 20, tzinfo=dt.UTC)
BOUNDARY = int(NOW.timestamp() * 1000)


def signal():
    from bybitbot.spread.execution import PairSignal

    return PairSignal(
        pair_trade_id=PAIR,
        pair_key=f"{A}:{B}",
        strategy_id="spread-1",
        symbol_a=A,
        symbol_b=B,
        direction="SHORT_A_LONG_B",
        notional=D("1000"),
        passive_price=D("100"),
        active_price=D("100"),
        qty_step_a=D("0.001"),
        qty_step_b=D("0.001"),
        side_a="Buy",
        side_b="Sell",
        passive_timeout_ms=2000,
        recovery_window_ms=5000,
        max_slippage_bps=D("50"),
        max_unhedged_notional=D("50"),
    )


def fresh_adapter():
    adapter = PrivateExecutionAdapter(PAIR, A, B)
    adapter.register_order(OrderIdentity(PAIR + "_A0", LEG_A, D("10")))
    adapter.register_order(OrderIdentity(PAIR + "_B1", LEG_B, D("10")))
    return adapter


def open_order(link: str, symbol: str, *, status: str = "New", filled: str = "0") -> OrderView:
    return OrderView(
        order_id="order-" + link,
        order_link_id=link,
        category="linear",
        symbol=symbol,
        side="Buy",
        order_type="Limit",
        status=status,
        price=D("100"),
        qty=D("10"),
        qty_filled=D(filled),
    )


def exec_view(exec_id: str, link: str, symbol: str, *, qty: str = "1") -> ExecutionView:
    return ExecutionView(
        exec_id=exec_id,
        order_id="order-" + link,
        order_link_id=link,
        category="linear",
        symbol=symbol,
        side="Buy",
        exec_price=D("100"),
        exec_qty=D(qty),
        exec_time=NOW,
    )


def position(symbol: str) -> PositionView:
    return PositionView(category="linear", symbol=symbol, size=D("0"))


def snapshot(*, boundary_ms: int = BOUNDARY, executions=(), extra_orders=()) -> CalibrationSnapshot:
    orders = tuple(
        [open_order(link, A if "_A" in link else B) for link in (PAIR + "_A0", PAIR + "_B1")]
    ) + tuple(extra_orders)
    return CalibrationSnapshot(
        orders=orders,
        executions=tuple(executions),
        positions=(position(A), position(B)),
        boundary_ms=boundary_ms,
        observed_at=NOW,
    )


def execution_update(
    exec_id: str,
    link: str,
    symbol: str,
    qty: D = D("1"),
    at=NOW + dt.timedelta(milliseconds=5),
):
    return ExecutionUpdate(
        exec_id=exec_id,
        order_id="order-" + link,
        order_link_id=link,
        category="linear",
        symbol=symbol,
        side="Buy",
        exec_price=D("100"),
        exec_qty=qty,
        event_time=at,
    )


def order_update(link: str, symbol: str, *, status="New", filled=D("0")):
    return OrderUpdate(
        order_id="order-" + link,
        order_link_id=link,
        category="linear",
        symbol=symbol,
        status=status,
        qty_filled=filled,
        qty=D("10"),
        price=D("100"),
        event_time=NOW,
    )


# --------------------------------------------------------------------------
# 事件翻译与归约
# --------------------------------------------------------------------------
def test_private_execution_drives_leg_reducer_without_duplicate_hedge():
    adapter = fresh_adapter()
    adapter.calibrate(snapshot())
    execution = new_execution(signal(), now_ms=100)

    events = adapter.on_execution(execution_update("e1", PAIR + "_A0", A, D("3")))
    execution, actions = reduce_private_events(execution, events, at_ms=100)
    submits = [a for a in actions if a.kind == ACTION_SUBMIT]
    assert len(submits) == 1 and submits[0].qty == D("3")
    assert submits[0].order_link_id == PAIR + "_B1"

    # 重复推送吞掉，不再发第二笔对冲单。
    assert adapter.on_execution(execution_update("e1", PAIR + "_A0", A, D("3"))) == []
    # 部分成交继续累加，但不会重复触发Leg B首单（状态已 WORKING）。
    events = adapter.on_execution(execution_update("e2", PAIR + "_A0", A, D("2")))
    execution, actions = reduce_private_events(execution, events, at_ms=120)
    assert execution.leg_a.qty_filled == D("5")
    assert not [a for a in actions if a.kind == ACTION_SUBMIT]


def test_leg_b_execution_is_attributed_and_chased_like_polling_flow():
    adapter = fresh_adapter()
    adapter.calibrate(snapshot())
    execution = new_execution(signal(), now_ms=0)
    execution, _ = reduce_private_events(
        execution, adapter.on_execution(execution_update("e1", PAIR + "_A0", A, D("10"))), at_ms=100
    )
    adapter.register_order(OrderIdentity(PAIR + "_B2", LEG_B, D("4")))
    execution, actions = reduce_private_events(
        execution,
        adapter.on_execution(execution_update("e2", PAIR + "_B1", B, D("6"))),
        at_ms=120,
        quote_b=None,
    )
    # 缺报价时归约器不会发追单，只产出告警/反向平仓；这里验证的是事件归属与
    # 累计数量正确，真正的追单由现有纯归约器测试覆盖。
    assert execution.leg_b.qty_filled == D("6")
    assert adapter.leg_filled_qty(LEG_B) == D("6")
    # 缺报价时归约器只产出告警/反向平仓，不产生新的 SUBMIT。
    assert not [action for action in actions if action.kind == ACTION_SUBMIT]


def test_out_of_order_and_duplicate_order_snapshots_never_create_actions():
    adapter = fresh_adapter()
    adapter.calibrate(snapshot())
    adapter.on_execution(execution_update("e1", PAIR + "_A0", A, D("3")))
    # 完全重复的 order 推送被吞掉；更小的 cumExecQty 只更新状态，不改数量。
    assert adapter.on_order(order_update(PAIR + "_A0", A, filled=D("3"))) == []
    assert (
        adapter.on_order(order_update(PAIR + "_A0", A, status="PartiallyFilled", filled=D("1")))
        == []
    )
    assert adapter.leg_filled_qty(LEG_A) == D("3")


def test_unregistered_or_wrong_symbol_events_do_not_touch_pair():
    adapter = fresh_adapter()
    adapter.calibrate(snapshot())
    assert adapter.on_execution(execution_update("x", "other-trade_A0", A)) == []
    # 品种与腿不匹配属于异常事实，必须 fail-closed。
    assert adapter.on_execution(execution_update("x", PAIR + "_A0", B)) == []
    assert adapter.gate.blocked and not adapter.gate.allows_new_actions


# --------------------------------------------------------------------------
# fail-closed 与 REST 校准恢复
# --------------------------------------------------------------------------
def test_disconnect_blocks_new_actions_and_calibration_restores():
    adapter = fresh_adapter()
    adapter.calibrate(snapshot())
    assert adapter.gate.allows_new_actions
    adapter.invalidate("websocket disconnected")
    assert adapter.gate.blocked
    assert adapter.on_execution(execution_update("late", PAIR + "_A0", A, D("5"))) == []
    assert adapter.on_order(order_update(PAIR + "_A0", A, status="Filled", filled=D("5"))) == []

    adapter.calibrate(snapshot(executions=(exec_view("rest-1", PAIR + "_A0", A, qty="5"),)))
    assert adapter.gate.allows_new_actions
    events = adapter.on_execution(execution_update("fresh", PAIR + "_A0", A, D("1")))
    assert events and events[0].qty_filled == D("6")


def test_calibration_requires_all_three_rest_dimensions():
    adapter = fresh_adapter()
    incomplete = CalibrationSnapshot(
        orders=snapshot().orders,
        executions=(),
        positions=(position(A),),
        boundary_ms=0,
        observed_at=NOW,
    )
    with pytest.raises(EventAdapterError):
        adapter.calibrate(incomplete)
    assert adapter.gate.blocked


def test_unseen_push_fill_blocks_calibration_until_rest_confirms_it():
    adapter = fresh_adapter()
    adapter.calibrate(snapshot())
    adapter.on_execution(execution_update("e1", PAIR + "_A0", A, D("3")))
    adapter.invalidate("auth failed")
    with pytest.raises(EventAdapterError, match="未出现在 REST"):
        adapter.calibrate(snapshot())
    assert adapter.gate.blocked
    adapter.calibrate(snapshot(executions=(exec_view("e1", PAIR + "_A0", A, qty="3"),)))
    assert adapter.gate.allows_new_actions
    assert adapter.leg_filled_qty(LEG_A) == D("3")


def test_late_fill_after_reconnect_does_not_replay_or_double_count():
    adapter = fresh_adapter()
    adapter.calibrate(
        snapshot(boundary_ms=BOUNDARY, executions=(exec_view("e1", PAIR + "_A0", A, qty="3"),))
    )
    adapter.invalidate("heartbeat timeout")
    adapter.calibrate(
        snapshot(boundary_ms=BOUNDARY + 1, executions=(exec_view("e1", PAIR + "_A0", A, qty="3"),))
    )
    stale = execution_update("e2", PAIR + "_A0", A, D("9"), at=NOW + dt.timedelta(milliseconds=1))
    assert adapter.on_execution(stale) == []
    assert adapter.leg_filled_qty(LEG_A) == D("3")
    # 新时刻的推送正常入账，使用校准后的累计口径。
    fresh = execution_update("e3", PAIR + "_A0", A, D("2"), at=NOW + dt.timedelta(milliseconds=2))
    events = adapter.on_execution(fresh)
    assert events and events[0].qty_filled == D("5")


def test_order_snapshot_ahead_of_execution_stream_fails_closed():
    adapter = fresh_adapter()
    adapter.calibrate(snapshot())
    events = adapter.on_order(order_update(PAIR + "_A0", A, filled=D("2")))
    assert events == [] and adapter.gate.blocked


# --------------------------------------------------------------------------
# WebSocket 层：linear 类别、鉴权与心跳
# --------------------------------------------------------------------------
async def test_private_stream_accepts_linear_category():
    client = BybitWebSocketClient(url="wss://stream.bybit.com/v5/private")
    stream = BybitPrivateStream(client)
    stream._running = True
    item = {
        "category": "linear",
        "orderId": "order-1",
        "orderLinkId": PAIR + "_A0",
        "symbol": A,
        "side": "Buy",
        "execId": "linear-1",
        "execPrice": "100",
        "execQty": "1",
        "execFee": "0",
        "execTime": str(int(NOW.timestamp() * 1000)),
        "execType": "Trade",
    }
    await stream._dispatch({"topic": "execution", "data": [item]})
    iterator = stream.execution_updates("linear")
    update = await anext(iterator)
    assert update.symbol == A and update.category == "linear"
    await iterator.aclose()


async def test_private_auth_failure_keeps_gate_closed():
    client = BybitWebSocketClient(
        url="wss://stream.bybit.com/v5/private",
        api_key="synthetic-key",
        api_secret="synthetic-secret",
    )
    client.send = AsyncMock()
    client.recv = AsyncMock(return_value=json.dumps({"op": "auth", "success": False}))
    with pytest.raises(WebSocketError):
        await client.auth()
    assert not client.authenticated


async def test_private_heartbeat_timeout_invalidates_stream(monkeypatch):
    client = BybitWebSocketClient(
        url="wss://stream.bybit.com/v5/private", ping_interval=0.01, ping_timeout=0.01
    )
    stream = BybitPrivateStream(client)
    stream._running = True
    client.recv = AsyncMock(side_effect=asyncio_timeout())
    with pytest.raises(WebSocketError, match="心跳超时"):
        await stream._consume()
    assert stream.generation >= 1 and stream.needs_reconciliation


def asyncio_timeout():

    return TimeoutError()
