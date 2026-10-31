"""Bybit V5 WebSocket 适配器（私有流 + 公开盘口）。

需求约束：
    * **MKT-02** 盘口快照/增量出现断档时重建，重建前不得使用不完整数据批准订单。
    * **MKT-03** 订阅重连后重新验证行情及账户快照，不能把「连接成功」视为
      「数据已恢复」。
    * **EXE-03** 重复及乱序推送、撤单与成交竞态、晚到成交；不重复记账、
      不错误回退终态。
    * **NFR-06** 超时和退避采用单调时钟；重连需要退避。

``websockets``（异步）维护按价格排序的盘口。普通 V5 盘口的 ``seq`` 与
``u`` 不保证逐一连续；只对倒退、重启、无快照、异常或过期数据置为无效。
连接中断后清除可信状态，必须收到新的全量快照才能继续。

私有流要点
----------
    * 鉴权：``{"op":"auth","args":[api_key, expires, signature]}``，
      签名串是 ``GET/realtime{expires}``（HMAC-SHA256）。
    * 订单事件里可能同时出现两条 ``orderStatus=Filled``（撤单请求被接受
      但订单同时成交），必须按 ``orderId + orderStatus + cumExecQty`` 去重。
    * 成交事件用 ``execId`` 去重（LED-01 的去重域）。
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import hashlib
import hmac
import json
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import websockets

from bybitbot.adapters.base import (
    ExecutionUpdate,
    OrderBook,
    OrderUpdate,
)
from bybitbot.adapters.bybit_rest import (
    BybitRestError,
    _levels,
    _parse_execution,
    _parse_order,
    _positive_int,
    _timestamp,
)
from bybitbot.clock import backoff_delay, now_utc

logger = logging.getLogger(__name__)


class WebSocketError(RuntimeError):
    """WebSocket 层错误。"""


@dataclass
class _BookState:
    """本地盘口状态。"""

    bids: dict[Decimal, Decimal] = field(default_factory=dict)
    asks: dict[Decimal, Decimal] = field(default_factory=dict)
    seq: int | None = None
    update_id: int | None = None
    source_ts: dt.datetime | None = None
    received_at: dt.datetime | None = None
    gap: bool = True


class BybitWebSocketClient:
    """V5 WebSocket 客户端。

    只负责连接、鉴权、订阅与重连；业务解析交给上层。
    """

    def __init__(
        self,
        *,
        url: str,
        api_key: str | None = None,
        api_secret: str | None = None,
        proxy: str | None = None,
        ping_interval: float = 20.0,
        ping_timeout: float = 10.0,
        reconnect_attempts: int = 0,  # 0 = 无限重连
    ) -> None:
        if (api_key or api_secret) and (
            not api_key or not api_secret or not url.startswith("wss://")
        ):
            raise ValueError("Private WebSocket requires both credentials and WSS")
        self.url = url
        self.api_key = api_key
        self.api_secret = api_secret
        self.proxy = proxy
        self.ping_interval = ping_interval
        self.ping_timeout = ping_timeout
        self.reconnect_attempts = reconnect_attempts
        self._ws: Any = None
        self._authenticated = False
        self._subscriptions: list[str] = []
        #: 重连次数；每次重连后上层必须重新验证数据（MKT-03）。
        self.reconnect_count = 0

    @property
    def authenticated(self) -> bool:
        return self._authenticated

    async def connect(self) -> None:
        kwargs: dict[str, Any] = {
            "ping_interval": self.ping_interval,
            "ping_timeout": self.ping_timeout,
            "max_size": 2**22,
            "proxy": self.proxy,  # None disables environment proxy discovery.
        }
        self._authenticated = False
        self._ws = await websockets.connect(self.url, **kwargs)

    async def disconnect(self) -> None:
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
            self._ws = None
        self._authenticated = False

    async def auth(self) -> None:
        if not self.api_key or not self.api_secret:
            self._authenticated = False
            return
        expires = int(time.time() * 1000) + 1000
        payload = f"GET/realtime{expires}"
        signature = hmac.new(self.api_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        await self.send({"op": "auth", "args": [self.api_key, expires, signature]})
        # 等待鉴权结果
        raw = await asyncio.wait_for(self.recv(), timeout=self.ping_timeout)
        try:
            message = json.loads(raw)
        except (ValueError, UnicodeError):
            raise WebSocketError("私有流鉴权响应无效") from None
        if (
            not isinstance(message, dict)
            or message.get("op") != "auth"
            or message.get("success") is not True
        ):
            # A response body may echo the supplied key: never log it.
            raise WebSocketError("私有流鉴权失败")
        self._authenticated = True

    async def subscribe(self, args: list[str]) -> None:
        self._subscriptions = list(args)
        await self.send({"op": "subscribe", "args": args})

    async def send(self, message: dict[str, Any]) -> None:
        if self._ws is None:
            raise WebSocketError("尚未连接")
        await self._ws.send(json.dumps(message))

    async def recv(self) -> str:
        if self._ws is None:
            raise WebSocketError("尚未连接")
        return await self._ws.recv()

    async def ping(self) -> None:
        with contextlib.suppress(Exception):
            await self.send({"op": "ping"})


class BybitPrivateStream:
    """私有流：订单与成交事件。

    提供两个异步迭代器，供执行器消费。
    """

    def __init__(self, client: BybitWebSocketClient) -> None:
        self.client = client
        self._order_queues: dict[str, asyncio.Queue[OrderUpdate]] = {
            category: asyncio.Queue(maxsize=10_000) for category in ("spot", "option")
        }
        self._execution_queues: dict[str, asyncio.Queue[ExecutionUpdate]] = {
            category: asyncio.Queue(maxsize=10_000) for category in ("spot", "option")
        }
        self._task: asyncio.Task | None = None
        self._running = False
        #: 最近一次收到的 orderId + orderStatus + cumExecQty，用于去重
        #: （Bybit 可能在撤单竞态下推两条 Filled）。
        self._seen_order_states: dict[str, tuple[str, str]] = {}
        self._seen_exec_ids: set[tuple[str, str]] = set()
        self.needs_reconciliation = True
        self.connected = False

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run(), name="bybit-private-stream")

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self.connected = False
        self.needs_reconciliation = True
        await self.client.disconnect()

    async def _run(self) -> None:
        attempt = 0
        while self._running:
            try:
                await self.client.connect()
                await self.client.auth()
                if not self.client.authenticated:
                    raise WebSocketError("私有流未鉴权")
                await self.client.subscribe(["order", "execution"])
                self.connected = True
                self.needs_reconciliation = True
                self._seen_order_states.clear()
                self._seen_exec_ids.clear()
                attempt = 0
                logger.info("私有流已连接，等待账户重新对账")
                await self._consume()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.connected = False
                self.needs_reconciliation = True
                attempt += 1
                if self.client.reconnect_attempts and attempt > self.client.reconnect_attempts:
                    logger.error("私有流重连次数超限，停止：%s", type(exc).__name__)
                    return
                delay = backoff_delay(attempt - 1, base=1.0, cap=30.0)
                logger.warning(
                    "私有流断开（第 %s 次），%.1fs 后重连：%s",
                    attempt,
                    delay,
                    type(exc).__name__,
                )
                await asyncio.sleep(delay)
            finally:
                self.connected = False
                self.needs_reconciliation = True
                self.client.reconnect_count += 1
                await self.client.disconnect()

    async def _consume(self) -> None:
        while self._running:
            raw = await self.client.recv()
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                self.needs_reconciliation = True
                raise WebSocketError("私有流 JSON 无效") from None
            if not isinstance(message, dict):
                raise WebSocketError("私有流消息无效")
            await self._dispatch(message)

    async def _dispatch(self, message: dict[str, Any]) -> None:
        topic = message.get("topic")
        if topic in {"order", "execution"}:
            rows = message.get("data")
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                self.needs_reconciliation = True
                raise WebSocketError("私有流数据列表无效")
            for item in rows:
                update = (
                    self._parse_order(item) if topic == "order" else self._parse_execution(item)
                )
                if update is not None:
                    queues = self._order_queues if topic == "order" else self._execution_queues
                    try:
                        queues[update.category].put_nowait(update)
                    except asyncio.QueueFull:
                        self.needs_reconciliation = True
                        raise WebSocketError("私有流处理积压，必须重新对账") from None

    def _parse_order(self, item: dict[str, Any]) -> OrderUpdate | None:
        category = item.get("category")
        if category not in {"spot", "option"}:
            return None
        try:
            view = _parse_order(category, item)
        except (BybitRestError, ValueError, TypeError):
            self.needs_reconciliation = True
            raise WebSocketError("私有订单消息字段无效") from None
        key = f"{category}:{view.order_id}"
        fingerprint = (view.status, str(view.qty_filled))
        if self._seen_order_states.get(key) == fingerprint:
            return None
        if len(self._seen_order_states) >= 10_000:
            # Durable execution/order dedupe remains in PostgreSQL. This cache
            # is only a bounded traffic optimization, never delivery evidence.
            self._seen_order_states.clear()
        self._seen_order_states[key] = fingerprint
        return OrderUpdate(
            order_id=view.order_id,
            order_link_id=view.order_link_id,
            category=category,
            symbol=view.symbol,
            status=view.status,
            qty_filled=view.qty_filled,
            qty=view.qty,
            price=view.price,
            event_time=view.updated_at,
            raw=item,
        )

    def _parse_execution(self, item: dict[str, Any]) -> ExecutionUpdate | None:
        category = item.get("category")
        if category not in {"spot", "option"}:
            return None
        try:
            view = _parse_execution(category, item)
        except (BybitRestError, ValueError, TypeError):
            self.needs_reconciliation = True
            raise WebSocketError("私有成交消息字段无效") from None
        key = (category, view.exec_id)
        if key in self._seen_exec_ids:
            return None
        if len(self._seen_exec_ids) >= 10_000:
            self._seen_exec_ids.clear()
        self._seen_exec_ids.add(key)
        return ExecutionUpdate(
            exec_id=view.exec_id,
            order_id=view.order_id,
            order_link_id=view.order_link_id,
            category=category,
            symbol=view.symbol,
            side=view.side,
            exec_price=view.exec_price,
            exec_qty=view.exec_qty,
            exec_fee=view.exec_fee,
            exec_fee_asset=view.exec_fee_asset,
            is_maker=view.is_maker,
            event_time=view.exec_time,
            raw=item,
        )

    async def order_updates(self, category: str) -> AsyncIterator[OrderUpdate]:
        if category not in self._order_queues:
            raise ValueError("Choose a supported product category")
        while self._running:
            yield await self._order_queues[category].get()

    async def execution_updates(self, category: str) -> AsyncIterator[ExecutionUpdate]:
        if category not in self._execution_queues:
            raise ValueError("Choose a supported product category")
        while self._running:
            yield await self._execution_queues[category].get()


class BybitOrderBookStream:
    """Rebuild standard V5 snapshots/deltas without assuming consecutive IDs.

    TCP preserves message order within one connection, but ``seq`` compares
    generation order across depths and is allowed to jump. After reconnect,
    backwards IDs, malformed levels or stale source time, only a fresh snapshot
    can restore trust. Call ``invalidate`` when the owning connection disconnects.
    """

    def __init__(
        self, client: BybitWebSocketClient, *, category: str, max_age_seconds: float = 10.0
    ) -> None:
        if category not in {"spot", "option"} or max_age_seconds <= 0:
            raise ValueError("Invalid book category or maximum age")
        self.client = client
        self.category = category
        self.max_age_seconds = max_age_seconds
        self._connection_generation = client.reconnect_count
        self._states: dict[str, _BookState] = {}

    def invalidate(self, symbol: str | None = None) -> None:
        """Invalidate cached state on connection loss or a known data gap."""
        states = [self._state(symbol)] if symbol else self._states.values()
        for state in states:
            state.gap = True

    def _state(self, symbol: str) -> _BookState:
        if self.client.reconnect_count != self._connection_generation:
            self.invalidate()
            self._connection_generation = self.client.reconnect_count
        return self._states.setdefault(symbol, _BookState())

    def _fields(self, message: dict[str, Any]) -> tuple[dict[str, Any], dt.datetime]:
        data = message.get("data", message)
        if not isinstance(data, dict):
            raise WebSocketError("盘口消息无效")
        try:
            source = _timestamp(message.get("ts")) or _timestamp(data.get("ts"))
        except BybitRestError:
            raise WebSocketError("盘口时间无效") from None
        moment = now_utc()
        if source is None or not -5 <= (moment - source).total_seconds() <= self.max_age_seconds:
            raise WebSocketError("盘口时间缺失或过期")
        return data, source

    def _validate(self, category: str, symbol: str, data: dict[str, Any]) -> None:
        if category != self.category or data.get("s") != symbol:
            raise WebSocketError("盘口品种不匹配")

    def apply_snapshot(self, category: str, symbol: str, message: dict[str, Any]) -> OrderBook:
        state = self._state(symbol)
        try:
            data, source = self._fields(message)
            self._validate(category, symbol, data)
            bids, asks = _levels(data.get("b")), _levels(data.get("a"))
            seq = _positive_int(data.get("seq"), "seq")
            update_id = _positive_int(data.get("u"), "u")
            if not bids or not asks or max(bids) >= min(asks):
                raise WebSocketError("盘口为空或交叉")
        except (BybitRestError, WebSocketError):
            state.gap = True
            raise WebSocketError("盘口快照无效，等待重建") from None
        state.bids, state.asks = bids, asks
        state.seq, state.update_id = seq, update_id
        state.source_ts, state.received_at, state.gap = source, now_utc(), False
        return self._snapshot(symbol, category)

    def apply_delta(self, category: str, symbol: str, message: dict[str, Any]) -> OrderBook | None:
        state = self._state(symbol)
        if state.gap:
            return None
        try:
            data, source = self._fields(message)
            self._validate(category, symbol, data)
            seq = _positive_int(data.get("seq"), "seq")
            update_id = _positive_int(data.get("u"), "u")
            if (
                update_id == 1
                or seq <= state.seq
                or update_id <= state.update_id
                or source < state.source_ts
            ):
                raise WebSocketError("盘口序号或时间倒退，等待新快照")
            bids, asks = (
                _levels(data.get("b"), allow_zero=True),
                _levels(data.get("a"), allow_zero=True),
            )
            next_bids, next_asks = state.bids.copy(), state.asks.copy()
            for changes, side in ((bids, next_bids), (asks, next_asks)):
                for price, size in changes.items():
                    if size == 0:
                        side.pop(price, None)
                    else:
                        side[price] = size
            if not next_bids or not next_asks or max(next_bids) >= min(next_asks):
                raise WebSocketError("盘口为空或交叉")
        except (BybitRestError, WebSocketError):
            state.gap = True
            return None
        state.bids, state.asks = next_bids, next_asks
        state.seq, state.update_id = seq, update_id
        state.source_ts, state.received_at = source, now_utc()
        return self._snapshot(symbol, category)

    def _snapshot(self, symbol: str, category: str) -> OrderBook:
        state = self._state(symbol)
        return OrderBook(
            category=category,
            symbol=symbol,
            bids=tuple(sorted(state.bids.items(), reverse=True)),
            asks=tuple(sorted(state.asks.items())),
            source_ts=state.source_ts,
            received_at=state.received_at,
            update_id=state.update_id,
            gap_detected=state.gap,
        )


__all__ = [
    "BybitOrderBookStream",
    "BybitPrivateStream",
    "BybitWebSocketClient",
    "WebSocketError",
]
