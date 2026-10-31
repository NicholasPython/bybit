"""Asynchronous Bybit V5 REST adapter.

GET signatures cover the exact encoded query bytes sent by httpx. Public market
requests never use credentials. Only reads retry; ambiguous write responses keep
the order uncertain, including duplicate IDs and unknown exchange error codes.

Field contracts: https://bybit-exchange.github.io/docs/v5/guide and the V5
market, account, user and order endpoint documentation. No margin purchasing
power or borrowed coins are exposed as spendable own cash.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import hashlib
import hmac
import ipaddress
import json
import os
import re
import time
from collections.abc import AsyncIterator
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from bybitbot.adapters.base import (
    AccountView,
    AdapterError,
    CancelAck,
    CoinBalance,
    ExecutionUpdate,
    ExecutionView,
    InstrumentSpec,
    InsufficientCapability,
    KeyInfo,
    OrderAck,
    OrderBook,
    OrderRejected,
    OrderRequest,
    OrderUncertain,
    OrderUpdate,
    OrderView,
    PositionView,
    RateLimited,
    Ticker,
    TransientError,
)
from bybitbot.clock import backoff_delay, now_utc

# These codes are retryable for READS, never for writes. 10002 gets a fresh
# signature timestamp on each attempt; clock skew still requires caller action.
RETRYABLE_RETCODES = frozenset({429, 10000, 10002, 10006, 10016, 10018, 170007})
_RATE_RETCODES = frozenset({429, 10006, 10018})
# An allowlist: an unfamiliar error must not release a possibly accepted order.
# https://bybit-exchange.github.io/docs/v5/error
_DEFINITE_WRITE_REJECTIONS = frozenset(
    {
        10001,
        10002,
        10003,
        10004,
        10005,
        10007,
        10008,
        10009,
        10010,
        10017,
        10024,
        10027,
        10028,
        10029,
        100028,
        110003,
        110004,
        110007,
        110012,
        110017,
        170131,
    }
)
ORDER_STATUSES = frozenset(
    {
        "New",
        "PartiallyFilled",
        "Untriggered",
        "Triggered",
        "Rejected",
        "PartiallyFilledCanceled",
        "PartiallyFilledCancelled",
        "Filled",
        "Cancelled",
        "Deactivated",
    }
)
_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)


class BybitRestError(AdapterError):
    """Invalid response or non-retryable read error; never contains raw bodies."""

    def __init__(self, message: str, *, ret_code: int | None = None) -> None:
        super().__init__(message)
        self.ret_code = ret_code


class _Result(dict[str, Any]):
    """Response-local metadata, without contaminating the exchange result map."""

    def __init__(
        self, value: dict[str, Any], *, source_ts: dt.datetime | None, received_at: dt.datetime
    ) -> None:
        super().__init__(value)
        self.source_ts = source_ts
        self.received_at = received_at


class BybitRestAdapter:
    """V5 REST, with explicitly enabled non-paper writes and bounded read retries."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None = None,
        api_secret: str | None = None,
        proxy: str | None = None,
        timeout: float = 30.0,
        recv_window_ms: int = 5000,
        read_max_attempts: int = 3,
        environment: str = "paper",
        allow_trade_writes: bool = False,
    ) -> None:
        if read_max_attempts < 1 or timeout <= 0 or recv_window_ms <= 0:
            raise ValueError("Request timeout, receive window and attempts must be positive")
        if environment not in {"paper", "demo", "testnet", "live"}:
            raise ValueError("Unrecognized adapter environment")
        endpoint = httpx.URL(base_url)
        hosts = {
            "paper": "api.bybit.com",
            "demo": "api-demo.bybit.com",
            "testnet": "api-testnet.bybit.com",
            "live": "api.bybit.com",
        }
        if (
            endpoint.scheme != "https"
            or endpoint.host != hosts[environment]
            or endpoint.userinfo
            or endpoint.port not in (None, 443)
            or endpoint.path not in ("", "/")
            or endpoint.query
            or endpoint.fragment
        ):
            raise ValueError("REST endpoint does not match adapter environment")
        self.base_url = base_url.rstrip("/")
        # Demo's public market data comes from mainnet; its private account
        # endpoints remain on api-demo with its independent credentials.
        self.public_base_url = "https://api.bybit.com" if environment == "demo" else self.base_url
        self.api_key = api_key if environment != "paper" else None
        self.api_secret = api_secret if environment != "paper" else None
        self.proxy = proxy
        self.timeout = timeout
        self.recv_window_ms = recv_window_ms
        self.read_max_attempts = read_max_attempts
        self.environment = environment
        self.allow_trade_writes = allow_trade_writes and environment != "paper"
        self._client: httpx.AsyncClient | None = None
        self._account_uid: str | None = None
        self._read_ready_at: dict[str, float] = {}
        self.rate_limits: dict[str, dict[str, int]] = {}

    async def __aenter__(self) -> BybitRestAdapter:
        self._client = httpx.AsyncClient(
            proxy=self.proxy,
            timeout=httpx.Timeout(self.timeout),
            trust_env=False,
            headers={"Accept": "application/json"},
        )
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("Adapter must be entered before sending requests")
        return self._client

    def _sign(self, timestamp_ms: str, payload: str) -> str:
        if not self.api_key or not self.api_secret:
            raise InsufficientCapability("Private REST requires an API key and secret")
        sign_data = f"{timestamp_ms}{self.api_key}{self.recv_window_ms}{payload}"
        return hmac.new(self.api_secret.encode(), sign_data.encode(), hashlib.sha256).hexdigest()

    def _auth_headers(self, payload: str) -> dict[str, str]:
        timestamp_ms = str(int(time.time() * 1000))
        return {
            "X-BAPI-API-KEY": self.api_key or "",
            "X-BAPI-SIGN": self._sign(timestamp_ms, payload),
            "X-BAPI-TIMESTAMP": timestamp_ms,
            "X-BAPI-RECV-WINDOW": str(self.recv_window_ms),
        }

    def _write_guard(self) -> None:
        # Tests cannot enable a live write, even by explicitly setting the
        # ordinary runtime switch. MockTransport testnet writes stay testable.
        if self.environment == "live" and (
            os.environ.get("BYBITBOT_TESTING") == "1" or "PYTEST_CURRENT_TEST" in os.environ
        ):
            raise InsufficientCapability("Live trade writes blocked in automated tests")
        if not self.allow_trade_writes or self.environment == "paper":
            raise OrderRejected("Trade writes disabled", ret_msg="trade writes disabled")

    def _record_limits(self, path: str, response: httpx.Response) -> float | None:
        values: dict[str, int] = {}
        for name in ("X-Bapi-Limit", "X-Bapi-Limit-Status", "X-Bapi-Limit-Reset-Timestamp"):
            try:
                values[name] = int(response.headers[name])
            except (KeyError, ValueError):
                continue
        if values:
            self.rate_limits[path] = values
        delay: float | None = None
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                delay = max(0.0, float(retry_after))
            except ValueError:
                with contextlib.suppress(TypeError, ValueError, OverflowError):
                    delay = max(
                        0.0, (parsedate_to_datetime(retry_after) - now_utc()).total_seconds()
                    )
        if "X-Bapi-Limit-Reset-Timestamp" in values and (
            values.get("X-Bapi-Limit-Status") == 0 or response.status_code == 429
        ):
            reset_delay = max(0.0, values["X-Bapi-Limit-Reset-Timestamp"] / 1000 - time.time())
            delay = max(delay or 0.0, reset_delay)
        if delay is not None:
            self._read_ready_at[path] = time.monotonic() + delay
        return delay

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> _Result:
        clean = {key: value for key, value in (params or {}).items() if value is not None}
        # Build the URL ONCE; do not encode a second, potentially different
        # representation for signing. Cursor '%' characters are opaque data.
        private = not path.startswith("/v5/market/")
        origin = self.base_url if private else self.public_base_url
        url = httpx.URL(f"{origin}{path}", params=clean)
        query = url.query.decode("ascii")
        for attempt in range(self.read_max_attempts):
            wait = self._read_ready_at.get(path, 0.0) - time.monotonic()
            if wait > 30:
                raise RateLimited(f"GET {path} cooldown", retry_after=wait)
            if wait > 0:
                await asyncio.sleep(wait)
            # Refresh signatures on every physical attempt, after waiting.
            headers = self._auth_headers(query) if private else {}
            retry_after: float | None = None
            try:
                response = await self.client.get(url, headers=headers)
                received_at = now_utc()
                retry_after = self._record_limits(path, response)
                if response.status_code == 429:
                    raise RateLimited(f"GET {path} HTTP 429", retry_after=retry_after)
                if response.status_code == 408 or response.status_code >= 500:
                    raise TransientError(f"GET {path} HTTP {response.status_code}")
                if response.status_code != 200:
                    # 403 can be an IP ban. Never hammer it with retries.
                    raise BybitRestError(f"GET {path} HTTP {response.status_code}")
                try:
                    body = response.json()
                except (ValueError, UnicodeError):
                    raise BybitRestError(f"GET {path} invalid JSON") from None
                result = self._unwrap(body, path)
                return _Result(
                    result,
                    source_ts=_timestamp(body.get("time")),
                    received_at=received_at,
                )
            except httpx.HTTPError:
                error: TransientError = TransientError(f"GET {path} transport failure")
            except TransientError as exc:
                error = exc
                if isinstance(exc, RateLimited):
                    retry_after = max(retry_after or 0.0, exc.retry_after or 0.0)
                    error = RateLimited(str(exc), retry_after=retry_after)
            if attempt + 1 == self.read_max_attempts:
                raise error from None
            if retry_after is not None and retry_after > 30:
                raise RateLimited(f"GET {path} cooldown", retry_after=retry_after) from None
            await self._sleep_backoff(attempt, retry_after=retry_after)
        raise AssertionError("unreachable")

    async def _post(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        self._write_guard()
        clean = {key: value for key, value in params.items() if value is not None}
        body = json.dumps(clean, separators=(",", ":"))
        headers = self._auth_headers(body)
        try:
            response = await self.client.post(
                f"{self.base_url}{path}",
                content=body.encode(),
                headers={**headers, "Content-Type": "application/json"},
            )
        except httpx.HTTPError:
            raise OrderUncertain(f"POST {path} transport failure") from None
        self._record_limits(path, response)
        if response.status_code != 200:
            raise OrderUncertain(f"POST {path} HTTP {response.status_code}")
        try:
            return self._unwrap(response.json(), path)
        except BybitRestError as exc:
            if exc.ret_code in _DEFINITE_WRITE_REJECTIONS:
                # Never include retMsg: exchanges may reflect request content.
                raise OrderRejected(
                    str(exc), ret_code=exc.ret_code, ret_msg="Exchange rejected request"
                ) from None
            raise OrderUncertain(str(exc)) from None
        except (TransientError, ValueError, UnicodeError):
            raise OrderUncertain(f"POST {path} ambiguous response") from None

    def _unwrap(self, body: Any, path: str) -> dict[str, Any]:
        if not isinstance(body, dict) or type(body.get("retCode")) is not int:
            raise BybitRestError(f"{path} missing or invalid retCode")
        code = body["retCode"]
        if code in _RATE_RETCODES:
            raise RateLimited(f"{path} retCode={code}")
        if code in RETRYABLE_RETCODES:
            raise TransientError(f"{path} retCode={code}")
        if code != 0:
            raise BybitRestError(f"{path} retCode={code}", ret_code=code)
        result = body.get("result")
        if not isinstance(result, dict):
            raise BybitRestError(f"{path} missing or invalid result")
        return result

    async def _sleep_backoff(self, attempt: int, *, retry_after: float | None = None) -> None:
        await asyncio.sleep(max(backoff_delay(attempt, base=0.5, cap=5.0), retry_after or 0.0))

    async def _pages(self, path: str, params: dict[str, Any]) -> AsyncIterator[_Result]:
        cursor: str | None = None
        seen: set[str] = set()
        for _ in range(1000):
            result = await self._get(path, {**params, "cursor": cursor})
            if "category" in result and result["category"] != params.get("category"):
                raise BybitRestError(f"{path} category mismatch")
            _items(result)
            yield result
            cursor = result.get("nextPageCursor")
            if cursor in (None, ""):
                return
            if not isinstance(cursor, str) or cursor in seen:
                raise BybitRestError(f"{path} invalid or repeated page cursor")
            seen.add(cursor)
        raise BybitRestError(f"{path} page limit exceeded; incomplete snapshot")

    async def get_server_time(self) -> dt.datetime:
        result = await self._get("/v5/market/time")
        if result.get("timeNano") not in (None, ""):
            return _scaled_timestamp(result["timeNano"], 1_000_000_000)
        if result.get("timeSecond") not in (None, ""):
            return _scaled_timestamp(result["timeSecond"], 1)
        raise BybitRestError("Server response missing timeSecond/timeNano")

    async def get_instruments(self, category: str) -> list[InstrumentSpec]:
        _category(category)
        params: dict[str, Any] = {"category": category}
        if category == "spot":
            # Spot expressly does NOT support limit/cursor.
            result = await self._get("/v5/market/instruments-info", params)
            return [_parse_instrument(category, item) for item in _items(result)]
        params.update(baseCoin="All", limit=1000)
        specs: dict[str, InstrumentSpec] = {}
        async for page in self._pages("/v5/market/instruments-info", params):
            for item in _items(page):
                spec = _parse_instrument(category, item)
                specs[spec.symbol] = spec
        return list(specs.values())

    async def _option_bases(self) -> list[str]:
        result = await self._get("/v5/market/option-base-coins")
        # Include hasSymbol=0: recently expired/delisted bases may have fills.
        return list(dict.fromkeys(_text(item, "baseCoin").upper() for item in _items(result)))

    async def get_ticker(self, category: str, symbol: str) -> Ticker:
        _category(category)
        result = await self._get("/v5/market/tickers", {"category": category, "symbol": symbol})
        items = _items(result)
        if len(items) != 1 or items[0].get("symbol") != symbol:
            raise BybitRestError("Ticker response missing requested symbol")
        return _parse_ticker(
            category, items[0], source_ts=result.source_ts, received_at=result.received_at
        )

    async def get_tickers(self, category: str, *, base_coin: str | None = None) -> list[Ticker]:
        """Bulk public quotes; option tickers require an explicit base or symbol."""
        _category(category)
        if category == "spot" and base_coin is not None:
            raise ValueError("base_coin is only supported for option tickers")
        bases = (
            [base_coin.upper()]
            if base_coin
            else await self._option_bases()
            if (category == "option")
            else [None]
        )
        tickers: dict[str, Ticker] = {}
        for base in bases:
            result = await self._get("/v5/market/tickers", {"category": category, "baseCoin": base})
            for item in _items(result):
                ticker = _parse_ticker(
                    category, item, source_ts=result.source_ts, received_at=result.received_at
                )
                tickers[ticker.symbol] = ticker
        return list(tickers.values())

    async def get_orderbook(self, category: str, symbol: str, limit: int = 50) -> OrderBook:
        _category(category)
        if limit < 1:
            raise ValueError("Orderbook depth must be positive")
        limit = min(limit, 25 if category == "option" else 1000)
        result = await self._get(
            "/v5/market/orderbook", {"category": category, "symbol": symbol, "limit": limit}
        )
        if result.get("s") != symbol:
            raise BybitRestError("Orderbook symbol mismatch")
        bids = _levels(result.get("b"))
        asks = _levels(result.get("a"))
        if bids and asks and max(bids) >= min(asks):
            raise BybitRestError("Crossed or locked orderbook")
        return OrderBook(
            category=category,
            symbol=symbol,
            bids=tuple(sorted(bids.items(), reverse=True)),
            asks=tuple(sorted(asks.items())),
            source_ts=_timestamp(result.get("ts")) or result.source_ts,
            received_at=result.received_at,
            update_id=_positive_int(result.get("u"), "u"),
        )

    async def get_account(self) -> AccountView:
        if self._account_uid is None:
            await self.get_key_info()
        info = await self._get("/v5/account/info")
        if info.get("marginMode") not in {
            "ISOLATED_MARGIN",
            "REGULAR_MARGIN",
            "PORTFOLIO_MARGIN",
        } or info.get("unifiedMarginStatus") not in {3, 4, 5, 6}:
            raise InsufficientCapability("Unrecognized or non-unified account mode")
        result = await self._get("/v5/account/wallet-balance", {"accountType": "UNIFIED"})
        rows = _items(result)
        if len(rows) != 1 or rows[0].get("accountType") != "UNIFIED":
            raise BybitRestError("Wallet response missing UNIFIED account")
        row = rows[0]
        coin_items = row.get("coin")
        if not isinstance(coin_items, list):
            raise BybitRestError("Wallet response missing coin array")
        coins: dict[str, CoinBalance] = {}
        for item in coin_items:
            if not isinstance(item, dict):
                raise BybitRestError("Invalid wallet coin entry")
            coin = _text(item, "coin").upper()
            if coin in coins:
                raise BybitRestError("Duplicate wallet coin entry")
            wallet = _number(item, "walletBalance")
            spot_borrow = _number(item, "spotBorrow", nonnegative=True)
            # Official: Old walletBalance = New walletBalance - spotBorrow.
            # borrowAmount ALREADY includes spotBorrow, plus derivatives debt.
            own_cash = wallet - spot_borrow
            borrowed = max(_number(item, "borrowAmount", nonnegative=True), spot_borrow)
            interest = _number(item, "accruedInterest", nonnegative=True)
            locked = _number(item, "locked", nonnegative=True)
            bonus = _opt_dec(item.get("bonus"))
            order_im = _opt_dec(item.get("totalOrderIM"))
            position_im = _opt_dec(item.get("totalPositionIM"))
            available = Decimal(0)
            # Portfolio IM can be "". Without a quantifiable per-coin hold,
            # refuse spendability rather than use USD collateral buying power.
            if (
                borrowed == 0
                and interest == 0
                and all(
                    value is not None and value >= 0 for value in (bonus, order_im, position_im)
                )
            ):
                available = max(own_cash - locked - bonus - order_im - position_im, Decimal(0))
            coins[coin] = CoinBalance(
                coin=coin,
                equity=_number(item, "equity"),
                available=available,
                wallet_balance=own_cash,
                usd_value=_opt_dec(item.get("usdValue")),
                borrowed=borrowed,
                accrued_interest=interest,
                locked=locked,
            )
        total_equity = _opt_dec(row.get("totalEquity"))
        return AccountView(
            account_uid=self._account_uid or "",
            account_mode="unified",
            coins=coins,
            total_equity=total_equity,
            exchange_equity=total_equity,
            source_ts=result.source_ts,
            received_at=result.received_at,
            raw={
                "accountType": row["accountType"],
                "marginMode": info["marginMode"],
                "unifiedMarginStatus": info["unifiedMarginStatus"],
                "cash_basis": "walletBalance-spotBorrow",
                "liabilities_in_cash": "spotBorrow",
            },
        )

    async def get_positions(self, category: str) -> list[PositionView]:
        _category(category)
        if category == "spot":
            return []  # Spot inventory is the coin balance, not /position/list.
        positions: dict[tuple[str, int], PositionView] = {}
        # No baseCoin returns ALL option positions (unlike execution/list).
        async for result in self._pages("/v5/position/list", {"category": category, "limit": 200}):
            for item in _items(result):
                size = _number(item, "size", nonnegative=True)
                if size == 0:
                    continue
                side = _text(item, "side")
                if side not in {"Buy", "Sell"}:
                    raise BybitRestError("Invalid position side")
                position = PositionView(
                    category=category,
                    symbol=_text(item, "symbol"),
                    position_idx=int(item.get("positionIdx") or 0),
                    size=size,
                    side=side,
                    avg_price=_opt_dec(item.get("avgPrice")),
                    mark_price=_opt_dec(item.get("markPrice")),
                    delta=_opt_dec(item.get("delta")),
                    gamma=_opt_dec(item.get("gamma")),
                    vega=_opt_dec(item.get("vega")),
                    theta=_opt_dec(item.get("theta")),
                    unrealised_pnl=_opt_dec(item.get("unrealisedPnl")),
                    realised_pnl=_opt_dec(item.get("curRealisedPnl")),
                    settle_coin=str(item["settleCoin"]).upper() if item.get("settleCoin") else None,
                    delivery_time=_timestamp(item.get("deliveryTime")),
                    raw=item,
                )
                positions[(position.symbol, position.position_idx)] = position
        return list(positions.values())

    async def get_open_orders(self, category: str) -> list[OrderView]:
        _category(category)
        views: dict[str, OrderView] = {}
        async for page in self._pages(
            "/v5/order/realtime", {"category": category, "openOnly": 0, "limit": 50}
        ):
            for item in _items(page):
                view = _parse_order(category, item)
                views[view.order_id] = view
        return list(views.values())

    async def get_order_history(
        self, category: str, *, order_link_ids: list[str] | None = None
    ) -> list[OrderView]:
        """Query every requested ID and page; absence never proves rejection.

        Bybit's default history window is seven days, with shorter retention for
        some cancelled/rejected orders. This method does not claim archival completeness.
        """
        _category(category)
        views: dict[str, OrderView] = {}
        bases = (
            await self._option_bases()
            if category == "option" and order_link_ids is None
            else [None]
        )
        if category == "option" and not bases:
            raise InsufficientCapability("Empty option base registry; history coverage unknown")
        for base in bases:
            for link_id in _link_filters(order_link_ids):
                params = {
                    "category": category,
                    "limit": 50,
                    "orderLinkId": link_id,
                    "baseCoin": base,
                }
                async for result in self._pages("/v5/order/history", params):
                    for item in _items(result):
                        view = _parse_order(category, item)
                        if link_id is not None and view.order_link_id != link_id:
                            raise BybitRestError("Order history filter mismatch")
                        views[view.order_id] = view
        return list(views.values())

    async def get_executions(
        self,
        category: str,
        *,
        order_link_ids: list[str] | None = None,
        base_coin: str | None = None,
        start_time: dt.datetime | None = None,
        end_time: dt.datetime | None = None,
    ) -> list[ExecutionView]:
        """Complete paginated windows, all IDs and all option bases.

        Bybit caps a time window at seven days and retained execution history at
        two years. Older starts fail explicitly, rather than return an empty
        window that reconciliation could mistake for complete history.
        """
        _category(category)
        filters = _link_filters(order_link_ids)
        if not filters:
            return []
        if base_coin is not None and category != "option":
            raise ValueError("base_coin is only supported for option executions")
        windows = _execution_windows(start_time, end_time)
        # orderLinkId overrides baseCoin per the official endpoint contract.
        if order_link_ids is not None:
            bases: list[str | None] = [None]
        elif base_coin:
            bases = [base_coin.upper()]
        elif category == "option":
            bases = await self._option_bases()
            if not bases:
                raise InsufficientCapability("Empty option base registry; history coverage unknown")
        else:
            bases = [None]
        if len(bases) * len(filters) * len(windows) > 10_000:
            raise InsufficientCapability("Execution recovery query exceeds bounded capacity")
        views: dict[tuple[str, str], ExecutionView] = {}
        for start_ms, end_ms in windows:
            for base in bases:
                for link_id in filters:
                    params = {
                        "category": category,
                        "limit": 100,
                        "baseCoin": base,
                        "orderLinkId": link_id,
                        "startTime": start_ms,
                        "endTime": end_ms,
                    }
                    async for result in self._pages("/v5/execution/list", params):
                        for item in _items(result):
                            view = _parse_execution(category, item)
                            if link_id is not None and view.order_link_id != link_id:
                                raise BybitRestError("Execution filter mismatch")
                            event_ms = int(view.exec_time.timestamp() * 1000)
                            if start_ms is not None and not start_ms <= event_ms <= end_ms:
                                raise BybitRestError("Execution outside requested time window")
                            views[(category, view.exec_id)] = view
        return list(views.values())

    async def get_key_info(self) -> KeyInfo:
        info = await self._get("/v5/user/query-api")
        permissions = info.get("permissions")
        if not isinstance(permissions, dict) or type(info.get("readOnly")) is not int:
            raise BybitRestError("Key info missing permissions/readOnly")
        if info["readOnly"] not in {0, 1}:
            raise BybitRestError("Key info has invalid readOnly")
        collected: set[str] = set()
        for entries in permissions.values():
            if not isinstance(entries, list) or any(not isinstance(x, str) for x in entries):
                raise BybitRestError("Key info has invalid permission group")
            collected.update(entries)
        ips = info.get("ips")
        if not isinstance(ips, list) or any(not isinstance(ip, str) for ip in ips):
            raise BybitRestError("Key info has invalid IP restrictions")
        # Empty/wildcard restrictions cannot satisfy the startup IP-binding check.
        restrictions: list[str] = []
        unrestricted = False
        for value in ips:
            if value in {"", "*"}:
                unrestricted = True
                continue
            try:
                network = ipaddress.ip_network(value, strict=False)
            except ValueError:
                raise BybitRestError("Key info has invalid IP restriction") from None
            if network.prefixlen != network.max_prefixlen or network.network_address.is_unspecified:
                unrestricted = True
            else:
                restrictions.append(value)
        # Any wildcard makes the whole key unrestricted, even alongside real IPs.
        if unrestricted:
            restrictions.clear()
        uid = info.get("userID")
        if type(uid) not in {int, str} or not str(uid).isdigit() or int(uid) <= 0:
            raise BybitRestError("Key info missing userID")
        self._account_uid = str(uid)
        return KeyInfo(
            api_key_prefix="***",  # Do not expose even short keys in full.
            permissions=frozenset(collected),
            ip_restrictions=tuple(restrictions),
            read_only=info["readOnly"] == 1,
            withdraw_enabled="Withdraw" in collected,
            account_uid=self._account_uid,
        )

    async def create_order(self, request: OrderRequest) -> OrderAck:
        self._write_guard()
        _category(request.category)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,36}", request.order_link_id):
            raise ValueError(
                "orderLinkId must contain 1-36 letters, digits, underscores or hyphens"
            )
        if request.order_type.value != "Limit":
            raise ValueError("Only limit orders have a verified quantity and price contract")
        if not request.qty.is_finite() or request.qty <= 0 or request.price is None:
            raise ValueError("Limit orders require positive quantity and price")
        if not request.price.is_finite() or request.price <= 0:
            raise ValueError("Limit price must be finite and positive")
        if request.close_on_trigger or request.position_idx not in (None, 0):
            raise ValueError("Unsupported trigger or position mode")
        if request.category == "option" and request.reduce_only != (request.side.value == "Sell"):
            raise ValueError("Options support buy-open and reduce-only sell-close only")
        params: dict[str, Any] = {
            "category": request.category,
            "symbol": request.symbol,
            "side": request.side.value,
            "orderType": "Limit",
            "qty": _qty_str(request.qty),
            "price": format(request.price, "f"),
            "timeInForce": request.time_in_force.value,
            "orderLinkId": request.order_link_id,
        }
        if request.category == "spot":
            params["isLeverage"] = 0
        else:
            params["reduceOnly"] = request.reduce_only
            params["positionIdx"] = 0
        result = await self._post("/v5/order/create", params)
        if (
            not isinstance(result.get("orderId"), str)
            or not result["orderId"]
            or (result.get("orderLinkId") != request.order_link_id)
        ):
            raise OrderUncertain("Create acknowledgement missing or mismatching order identity")
        if "orderStatus" in result and result["orderStatus"] not in ORDER_STATUSES:
            raise OrderUncertain("Create acknowledgement has unrecognized order status")
        # REST acknowledges acceptance, not an exchange New/Filled observation.
        return OrderAck(
            order_id=result["orderId"],
            order_link_id=request.order_link_id,
            status="Accepted",
            raw=result,
        )

    async def cancel_order(
        self,
        category: str,
        symbol: str,
        *,
        order_id: str | None = None,
        order_link_id: str | None = None,
    ) -> CancelAck:
        self._write_guard()
        _category(category)
        if not order_id and not order_link_id:
            raise ValueError("Cancel requires orderId or orderLinkId")
        params: dict[str, Any] = {"category": category, "symbol": symbol}
        # Bybit gives orderId priority; do not pretend orderLinkId was checked too.
        params.update({"orderId": order_id} if order_id else {"orderLinkId": order_link_id})
        result = await self._post("/v5/order/cancel", params)
        if not isinstance(result.get("orderId"), str) or not result["orderId"]:
            raise OrderUncertain("Cancel acknowledgement missing orderId")
        if (
            order_id
            and result["orderId"] != order_id
            or (not order_id and result.get("orderLinkId") != order_link_id)
        ):
            raise OrderUncertain("Cancel acknowledgement order identity mismatch")
        return CancelAck(
            order_id=result["orderId"],
            order_link_id=str(result.get("orderLinkId") or order_link_id or ""),
            accepted=True,
            raw=result,
        )

    async def order_updates(self, category: str) -> AsyncIterator[OrderUpdate]:
        return
        yield  # pragma: no cover

    async def execution_updates(self, category: str) -> AsyncIterator[ExecutionUpdate]:
        return
        yield  # pragma: no cover


def _execution_windows(
    start_time: dt.datetime | None, end_time: dt.datetime | None
) -> list[tuple[int | None, int | None]]:
    if start_time is None and end_time is None:
        return [(None, None)]
    moment = now_utc()
    for value in (start_time, end_time):
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("Execution query times must be timezone-aware")
    end = (end_time or moment).astimezone(dt.UTC)
    start = (start_time or end - dt.timedelta(days=7)).astimezone(dt.UTC)
    if start > end or end > moment + dt.timedelta(seconds=5):
        raise ValueError("Invalid execution time interval")
    if start < moment - dt.timedelta(days=730):
        raise InsufficientCapability("Execution recovery starts outside retained two-year history")
    windows: list[tuple[int | None, int | None]] = []
    start_ms, end_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
    span_ms = 7 * 24 * 60 * 60 * 1000 - 1
    while start_ms <= end_ms:
        stop = min(start_ms + span_ms, end_ms)
        windows.append((start_ms, stop))
        start_ms = stop + 1
    return windows


def _category(category: str) -> None:
    if category not in {"spot", "option"}:
        raise InsufficientCapability("Only spot and option adapters are supported")


def _link_filters(ids: list[str] | None) -> list[str | None]:
    if ids is None:
        return [None]
    if any(not isinstance(link_id, str) or not link_id for link_id in ids):
        raise ValueError("Empty orderLinkId would broaden the query")
    return list(dict.fromkeys(ids))


def _items(result: dict[str, Any]) -> list[dict[str, Any]]:
    items = result.get("list")
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise BybitRestError("Response missing or invalid list")
    return items


def _text(item: dict[str, Any], key: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value:
        raise BybitRestError(f"Missing or invalid {key}")
    return value


def _opt_dec(value: Any) -> Decimal | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return parsed if parsed.is_finite() else None


def _number(item: dict[str, Any], key: str, *, nonnegative: bool = False) -> Decimal:
    value = _opt_dec(item.get(key))
    if value is None or nonnegative and value < 0:
        raise BybitRestError(f"Missing or invalid {key}")
    return value


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise BybitRestError(f"Invalid {field}")
    try:
        parsed = int(str(value))
    except (ValueError, TypeError):
        raise BybitRestError(f"Invalid {field}") from None
    if parsed < 1:
        raise BybitRestError(f"Invalid {field}")
    return parsed


def _scaled_timestamp(value: Any, units_per_second: int) -> dt.datetime:
    amount = _positive_int(value, "timestamp")
    seconds, remainder = divmod(amount, units_per_second)
    try:
        return _EPOCH + dt.timedelta(
            seconds=seconds, microseconds=remainder * 1_000_000 // units_per_second
        )
    except OverflowError:
        raise BybitRestError("Timestamp out of range") from None


def _timestamp(value: Any) -> dt.datetime | None:
    return None if value in (None, "", 0, "0") else _scaled_timestamp(value, 1000)


def _levels(value: Any, *, allow_zero: bool = False) -> dict[Decimal, Decimal]:
    if not isinstance(value, list):
        raise BybitRestError("Orderbook missing side array")
    result: dict[Decimal, Decimal] = {}
    for level in value:
        if not isinstance(level, (list, tuple)) or len(level) != 2:
            raise BybitRestError("Invalid orderbook level")
        price, size = _opt_dec(level[0]), _opt_dec(level[1])
        if price is None or price <= 0 or size is None or size < 0:
            raise BybitRestError("Invalid orderbook price/size")
        if price in result:
            raise BybitRestError("Duplicate orderbook price")
        if size > 0 or allow_zero:
            result[price] = size
    return result


def _qty_str(qty: Decimal) -> str:
    return format(qty.normalize(), "f")


def _parse_instrument(category: str, item: dict[str, Any]) -> InstrumentSpec:
    symbol = _text(item, "symbol")
    price_filter = item.get("priceFilter") or {}
    lot_filter = item.get("lotSizeFilter") or {}
    if not isinstance(price_filter, dict) or not isinstance(lot_filter, dict):
        raise BybitRestError("Invalid instrument filters")
    return InstrumentSpec(
        category=category,
        symbol=symbol,
        base_coin=_text(item, "baseCoin").upper(),
        quote_coin=_text(item, "quoteCoin").upper(),
        status=_text(item, "status"),
        tick_size=_opt_dec(price_filter.get("tickSize")),
        qty_step=_opt_dec(lot_filter.get("qtyStep")) or _opt_dec(lot_filter.get("basePrecision")),
        min_order_qty=_opt_dec(lot_filter.get("minOrderQty")),
        max_order_qty=_opt_dec(lot_filter.get("maxLimitOrderQty"))
        or _opt_dec(lot_filter.get("maxOrderQty")),
        min_order_amt=_opt_dec(lot_filter.get("minOrderAmt")),
        max_order_amt=_opt_dec(lot_filter.get("maxOrderAmt")),
        settle_coin=str(item["settleCoin"]).upper() if item.get("settleCoin") else None,
        options_type=item.get("optionsType")
        if item.get("optionsType") in {"Call", "Put"}
        else None,
        strike_price=_opt_dec(item.get("strikePrice"))
        or (_strike_from_symbol(symbol) if category == "option" else None),
        delivery_time=_timestamp(item.get("deliveryTime")),
        raw=item,
    )


def _strike_from_symbol(symbol: str) -> Decimal | None:
    parts = symbol.split("-")
    return _opt_dec(parts[2]) if len(parts) in {4, 5} and parts[3] in {"C", "P"} else None


def _parse_ticker(
    category: str,
    item: dict[str, Any],
    *,
    source_ts: dt.datetime | None = None,
    received_at: dt.datetime | None = None,
) -> Ticker:
    if source_ts is None:
        raise BybitRestError("Ticker response missing source time")
    return Ticker(
        category=category,
        symbol=_text(item, "symbol"),
        bid_price=_opt_dec(item.get("bid1Price")),
        bid_size=_opt_dec(item.get("bid1Size")),
        ask_price=_opt_dec(item.get("ask1Price")),
        ask_size=_opt_dec(item.get("ask1Size")),
        last_price=_opt_dec(item.get("lastPrice")),
        mark_price=_opt_dec(item.get("markPrice")),
        index_price=_opt_dec(item.get("indexPrice")),
        open_interest=_opt_dec(item.get("openInterest")),
        iv=_opt_dec(item.get("markIv")),
        delta=_opt_dec(item.get("delta")),
        gamma=_opt_dec(item.get("gamma")),
        vega=_opt_dec(item.get("vega")),
        theta=_opt_dec(item.get("theta")),
        source_ts=source_ts,
        received_at=received_at,
    )


def _parse_order(category: str, item: dict[str, Any]) -> OrderView:
    status = item.get("orderStatus")
    if status not in ORDER_STATUSES:
        raise BybitRestError("Unrecognized orderStatus")
    side = _text(item, "side")
    if side not in {"Buy", "Sell"}:
        raise BybitRestError("Unrecognized order side")
    qty, filled = (
        _number(item, "qty", nonnegative=True),
        _number(item, "cumExecQty", nonnegative=True),
    )
    if qty <= 0 or filled > qty:
        raise BybitRestError("Invalid order cumulative quantity")
    return OrderView(
        order_id=_text(item, "orderId"),
        order_link_id=str(item.get("orderLinkId") or ""),
        category=category,
        symbol=_text(item, "symbol"),
        side=side,
        order_type=_text(item, "orderType"),
        status=status,
        price=_opt_dec(item.get("price")),
        qty=qty,
        qty_filled=filled,
        time_in_force=item.get("timeInForce"),
        reduce_only=_optional_bool(item.get("reduceOnly")) or False,
        position_idx=int(item["positionIdx"]) if item.get("positionIdx") is not None else None,
        avg_price=_opt_dec(item.get("avgPrice")),
        created_at=_timestamp(item.get("createdTime")),
        updated_at=_timestamp(item.get("updatedTime")),
        reject_reason=item.get("rejectReason"),
        raw=item,
    )


def _optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if type(value) is not bool:
        raise BybitRestError("Invalid boolean response field")
    return value


def _parse_execution(category: str, item: dict[str, Any]) -> ExecutionView:
    event_time = _timestamp(item.get("execTime"))
    if event_time is None:
        raise BybitRestError("Execution missing execTime")
    side = _text(item, "side")
    if side not in {"Buy", "Sell"}:
        raise BybitRestError("Unrecognized execution side")
    if item.get("execType") not in {None, "Trade"}:
        raise InsufficientCapability("Non-trade execution requires separate settlement accounting")
    price, qty = _number(item, "execPrice"), _number(item, "execQty")
    if price <= 0 or qty <= 0:
        raise BybitRestError("Execution price and quantity must be positive")
    fee = _opt_dec(item.get("execFee"))
    fee_asset = str(item["feeCurrency"]).upper() if item.get("feeCurrency") else None
    if fee is None or fee != 0 and not fee_asset:
        raise BybitRestError("Execution fee amount or currency missing")
    return ExecutionView(
        exec_id=_text(item, "execId"),
        order_id=_text(item, "orderId"),
        order_link_id=str(item.get("orderLinkId") or "") or None,
        category=category,
        symbol=_text(item, "symbol"),
        side=side,
        exec_price=price,
        exec_qty=qty,
        exec_time=event_time,
        exec_fee=fee,
        exec_fee_asset=fee_asset,
        is_maker=_optional_bool(item.get("isMaker")),
        raw=item,
    )


__all__ = ["RETRYABLE_RETCODES", "BybitRestAdapter", "BybitRestError"]
