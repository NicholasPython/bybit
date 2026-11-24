"""Synthetic V5 contracts: every HTTP interaction uses MockTransport."""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import time
from contextlib import asynccontextmanager
from decimal import Decimal
from unittest.mock import AsyncMock

import httpx
import pytest

from bybitbot.adapters.base import (
    InsufficientCapability,
    OrderRejected,
    OrderRequest,
    OrderUncertain,
    RateLimited,
    TransientError,
)
from bybitbot.adapters.bybit_rest import ORDER_STATUSES, BybitRestAdapter, BybitRestError
from bybitbot.adapters.bybit_ws import (
    BybitOrderBookStream,
    BybitPrivateStream,
    BybitWebSocketClient,
    WebSocketError,
)
from bybitbot.clock import now_utc
from bybitbot.execution.states import ACCEPTED, effective_status
from bybitbot.settings.models import OrderSide, OrderType

D = Decimal
STAMP = 1_730_000_000_000


def response(result, *, code=0, headers=None, status=200):
    return httpx.Response(
        status,
        json={"retCode": code, "retMsg": "OK", "result": result, "time": STAMP},
        headers=headers,
    )


@asynccontextmanager
async def adapter(handler, *, environment="testnet", allow=True, attempts=3):
    origins = {
        "paper": "https://api.bybit.com",
        "live": "https://api.bybit.com",
        "testnet": "https://api-testnet.bybit.com",
        "demo": "https://api-demo.bybit.com",
    }
    instance = BybitRestAdapter(
        base_url=origins[environment],
        api_key="synthetic-key",
        api_secret="synthetic-secret",
        environment=environment,
        allow_trade_writes=allow,
        read_max_attempts=attempts,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        instance._client = client
        yield instance
        instance._client = None


def request(*, category="spot", side=OrderSide.BUY, reduce_only=False):
    symbol = {
        "spot": "BTCUSDT",
        "option": "BTC-30OCT26-90000-C",
        "linear": "BTCUSDT",
    }[category]
    return OrderRequest(
        category=category,
        symbol=symbol,
        side=side,
        order_type=OrderType.LIMIT,
        qty=D("0.01"),
        price=D("100"),
        order_link_id="contract-test-1",
        reduce_only=reduce_only,
        position_idx=0,
    )


def instrument(category="spot", symbol=None):
    if category == "spot":
        return {
            "symbol": symbol or "BTCUSDT",
            "baseCoin": "BTC",
            "quoteCoin": "USDT",
            "status": "Trading",
            "priceFilter": {"tickSize": "0.01"},
            "lotSizeFilter": {
                "basePrecision": "0.000001",
                "minOrderQty": "0.000001",
                "maxLimitOrderQty": "71.73",
                "maxOrderQty": "100",
                "minOrderAmt": "5",
            },
        }
    return {
        "symbol": symbol or "ETH-30OCT26-3000-C-USDT",
        "baseCoin": "ETH",
        "quoteCoin": "USDT",
        "settleCoin": "USDT",
        "status": "Trading",
        "optionsType": "Call",
        "deliveryTime": str(STAMP),
        "priceFilter": {"tickSize": "0.1"},
        "lotSizeFilter": {"qtyStep": "0.01", "minOrderQty": "0.01", "maxOrderQty": "100"},
    }


def order(link="one", *, category="spot", status="Filled"):
    return {
        "category": category,
        "orderId": f"order-{link}",
        "orderLinkId": link,
        "symbol": "BTCUSDT",
        "side": "Buy",
        "orderType": "Limit",
        "orderStatus": status,
        "price": "100",
        "qty": "1",
        "cumExecQty": "1",
        "reduceOnly": False,
        "createdTime": str(STAMP),
        "updatedTime": str(STAMP),
    }


def execution(link="one", *, category="spot", exec_id=None, stamp=STAMP):
    return {
        "category": category,
        "orderId": f"order-{link}",
        "orderLinkId": link,
        "symbol": "BTCUSDT",
        "side": "Buy",
        "execId": exec_id or f"exec-{link}",
        "execPrice": "100",
        "execQty": "1",
        "execFee": "0.001",
        "feeCurrency": "BTC",
        "isMaker": False,
        "execTime": str(stamp),
        "execType": "Trade",
    }


def key_info(**changes):
    return {
        "userID": 1234,
        "readOnly": 0,
        "permissions": {"Spot": ["SpotTrade"], "Options": ["OptionsTrade"]},
        "ips": ["192.0.2.10"],
        **changes,
    }


def coin(**changes):
    return {
        "coin": "USDT",
        "equity": "1200",
        "walletBalance": "1000",
        "spotBorrow": "0",
        "borrowAmount": "0",
        "accruedInterest": "0",
        "locked": "30",
        "bonus": "5",
        "totalOrderIM": "20",
        "totalPositionIM": "10",
        **changes,
    }


async def test_public_reads_have_no_auth_and_paper_discards_credentials():
    def handle(req):
        assert not any(name.lower().startswith("x-bapi") for name in req.headers)
        return response({"timeNano": "1730000000123456789"})

    async with adapter(handle, environment="paper") as instance:
        assert instance.api_key is None and instance.api_secret is None
        assert await instance.get_server_time() == dt.datetime.fromtimestamp(
            1_730_000_000, dt.UTC
        ).replace(microsecond=123456)


async def test_signed_get_uses_exact_encoded_request_bytes():
    def handle(req):
        payload = req.url.query.decode("ascii")
        assert req.url.params["cursor"] == "a%2B +/="
        assert req.url.params["orderLinkId"] == "z_1"
        raw = f"{req.headers['X-BAPI-TIMESTAMP']}synthetic-key5000{payload}"
        expected = hmac.new(b"synthetic-secret", raw.encode(), hashlib.sha256).hexdigest()
        assert req.headers["X-BAPI-SIGN"] == expected
        return response({"list": []})

    async with adapter(handle) as instance:
        await instance._get("/v5/order/history", {"orderLinkId": "z_1", "cursor": "a%2B +/="})


async def test_demo_routes_public_and_private_to_separate_hosts():
    hosts = []

    def handle(req):
        hosts.append(req.url.host)
        if req.url.path.startswith("/v5/market/"):
            assert "X-BAPI-API-KEY" not in req.headers
            return response({"timeSecond": "1730000000"})
        return response(key_info())

    async with adapter(handle, environment="demo") as instance:
        await instance.get_server_time()
        await instance.get_key_info()
    assert hosts == ["api.bybit.com", "api-demo.bybit.com"]


@pytest.mark.parametrize(
    "url,env",
    [
        ("https://api.bybit.com", "testnet"),
        ("http://api-testnet.bybit.com", "testnet"),
        ("https://api-testnet.bybit.com.evil.test", "testnet"),
        ("https://user@api-testnet.bybit.com", "testnet"),
    ],
)
def test_endpoint_mismatch_rejected(url, env):
    with pytest.raises(ValueError):
        BybitRestAdapter(base_url=url, environment=env)


async def test_spot_spec_has_no_pagination_and_uses_limit_precision():
    def handle(req):
        assert dict(req.url.params) == {"category": "spot"}
        return response({"category": "spot", "list": [instrument()]})

    async with adapter(handle) as instance:
        (spec,) = await instance.get_instruments("spot")
    assert spec.qty_step == D("0.000001")
    assert spec.max_order_qty == D("71.73")
    assert spec.min_order_amt == D("5")


async def test_option_specs_all_bases_paginated_and_strike_from_symbol():
    queries = []

    def handle(req):
        queries.append(dict(req.url.params))
        cursor = req.url.params.get("cursor")
        return response(
            {
                "category": "option",
                "list": [instrument("option", "ETH-30OCT26-3000-C-USDT")],
                "nextPageCursor": "opaque%/cursor" if cursor is None else "",
            }
        )

    async with adapter(handle) as instance:
        (spec,) = await instance.get_instruments("option")
    assert len(queries) == 2
    assert queries[0]["baseCoin"] == "All"
    assert queries[1]["cursor"] == "opaque%/cursor"
    assert spec.strike_price == D("3000")
    assert spec.options_type == "Call" and spec.settle_coin == "USDT"


async def test_repeated_cursor_fails_instead_of_partial_snapshot():
    async with adapter(
        lambda req: response({"list": [instrument("option")], "nextPageCursor": "same"})
    ) as instance:
        with pytest.raises(BybitRestError, match="repeated"):
            await instance.get_instruments("option")


async def test_option_bulk_ticker_mark_iv_and_depth_cap():
    def handle(req):
        if req.url.path.endswith("tickers"):
            assert req.url.params["baseCoin"] == "ETH"
            return response(
                {
                    "list": [
                        {
                            "symbol": "ETH-30OCT26-3000-C",
                            "markIv": "0.61",
                            "delta": "0.4",
                            "bid1Price": "100",
                            "ask1Price": "110",
                        }
                    ]
                }
            )
        assert req.url.params["limit"] == "25"
        return response(
            {
                "s": "ETH-30OCT26-3000-C",
                "u": 7,
                "ts": STAMP,
                "b": [["100", "2"]],
                "a": [["110", "3"]],
            }
        )

    async with adapter(handle) as instance:
        (ticker,) = await instance.get_tickers("option", base_coin="eth")
        book = await instance.get_orderbook("option", ticker.symbol, limit=1000)
    assert ticker.iv == D("0.61") and ticker.delta == D("0.4")
    assert ticker.source_ts and ticker.received_at
    assert book.ask() == D("110")


async def test_wallet_nested_net_cash_and_uid():
    def handle(req):
        if req.url.path.endswith("query-api"):
            return response(key_info())
        if req.url.path.endswith("/info"):
            return response({"marginMode": "REGULAR_MARGIN", "unifiedMarginStatus": 5})
        return response(
            {"list": [{"accountType": "UNIFIED", "coin": [coin()], "totalEquity": "1200"}]}
        )

    async with adapter(handle) as instance:
        account = await instance.get_account()
    assert account.account_uid == "1234" and account.account_mode == "unified"
    assert account.coin("USDT").wallet_balance == D("1000")
    assert account.coin("USDT").available == D("935")
    assert account.exchange_equity == D("1200")
    assert not account.has_debt


@pytest.mark.parametrize(
    "changes,own_cash,debt",
    [
        ({"spotBorrow": "100", "borrowAmount": "140"}, "900", "140"),
        ({"spotBorrow": "100", "borrowAmount": "0"}, "900", "100"),
    ],
)
async def test_wallet_debt_not_double_counted_or_spendable(changes, own_cash, debt):
    def handle(req):
        if req.url.path.endswith("query-api"):
            return response(key_info())
        if req.url.path.endswith("/info"):
            return response({"marginMode": "REGULAR_MARGIN", "unifiedMarginStatus": 5})
        return response({"list": [{"accountType": "UNIFIED", "coin": [coin(**changes)]}]})

    async with adapter(handle) as instance:
        account = await instance.get_account()
    assert account.coin("USDT").wallet_balance == D(own_cash)
    assert account.coin("USDT").borrowed == D(debt)
    assert account.has_debt and account.coin("USDT").available == 0


async def test_portfolio_unknown_holds_have_zero_spendable_cash():
    def handle(req):
        if req.url.path.endswith("query-api"):
            return response(key_info())
        if req.url.path.endswith("/info"):
            return response({"marginMode": "PORTFOLIO_MARGIN", "unifiedMarginStatus": 5})
        return response({"list": [{"accountType": "UNIFIED", "coin": [coin(totalOrderIM="")]}]})

    async with adapter(handle) as instance:
        assert (await instance.get_account()).coin("USDT").available == 0


async def test_blank_accrued_interest_on_non_collateral_coin_reads_as_zero():
    """真实账户返回空串应计利息（USD 等非保证金币种），不能视为解析失败。"""
    def handle(req):
        if req.url.path.endswith("query-api"):
            return response(key_info())
        if req.url.path.endswith("/info"):
            return response({"marginMode": "REGULAR_MARGIN", "unifiedMarginStatus": 5})
        return response(
            {
                "list": [
                    {
                        "accountType": "UNIFIED",
                        "coin": [
                            coin(),
                            {
                                "coin": "USD",
                                "equity": "0.009",
                                "walletBalance": "0.009",
                                "spotBorrow": "0",
                                "borrowAmount": "0",
                                "accruedInterest": "",
                                "availableToWithdraw": "",
                                "locked": "0",
                                "bonus": "0",
                                "totalOrderIM": "0",
                                "totalPositionIM": "0",
                                "marginCollateral": False,
                            },
                        ],
                        "totalEquity": "1200",
                    }
                ]
            }
        )

    async with adapter(handle) as instance:
        account = await instance.get_account()
    assert account.coin("USD").accrued_interest == D("0")
    assert account.coin("USDT").available == D("935")


@pytest.mark.parametrize(
    "coin_changes",
    [
        {"accruedInterest": "", "marginCollateral": True},
        {"accruedInterest": ""},
        {"accruedInterest": "abc"},
        {"accruedInterest": "-1"},
    ],
)
async def test_undecidable_accrued_interest_keeps_account_unreadable(coin_changes):
    """空串出现在保证金币种或字段无法判定时必须 fail-closed，而不是当作 0。"""
    def handle(req):
        if req.url.path.endswith("query-api"):
            return response(key_info())
        if req.url.path.endswith("/info"):
            return response({"marginMode": "REGULAR_MARGIN", "unifiedMarginStatus": 5})
        return response({"list": [{"accountType": "UNIFIED", "coin": [coin(**coin_changes)]}]})

    async with adapter(handle) as instance:
        with pytest.raises(BybitRestError, match="accruedInterest"):
            await instance.get_account()


@pytest.mark.parametrize(
    "change",
    [
        {"readOnly": "0"},
        {"userID": ""},
        {"ips": "192.0.2.1"},
        {"permissions": {"Spot": "SpotTrade"}},
        {"ips": ["not-an-address"]},
    ],
)
async def test_key_identity_fields_fail_closed(change):
    async with adapter(lambda req: response(key_info(**change))) as instance:
        with pytest.raises(BybitRestError):
            await instance.get_key_info()


async def test_wildcard_does_not_become_bound_because_real_ip_also_present():
    value = key_info(ips=["192.0.2.10", "*"], permissions={"Wallet": ["Withdraw"]})
    async with adapter(lambda req: response(value)) as instance:
        info = await instance.get_key_info()
    assert not info.ip_restrictions and info.withdraw_enabled


@pytest.mark.parametrize("ips", [["192.0.2.0/24"], ["::/0"], ["0.0.0.0", "192.0.2.10"]])  # noqa: S104
async def test_only_fixed_ip_key_binding_counts_as_bound(ips):
    async with adapter(lambda req: response(key_info(ips=ips))) as instance:
        assert not (await instance.get_key_info()).ip_restrictions


async def test_spot_positions_do_not_call_unsupported_api():
    def fail(req):
        raise AssertionError("spot positions must come from wallet")

    async with adapter(fail) as instance:
        assert await instance.get_positions("spot") == []


async def test_execution_ids_and_option_base_coverage():
    seen = []

    def handle(req):
        if req.url.path.endswith("option-base-coins"):
            return response(
                {"list": [{"baseCoin": "BTC", "hasSymbol": 1}, {"baseCoin": "ETH", "hasSymbol": 0}]}
            )
        base = req.url.params.get("baseCoin")
        link = req.url.params.get("orderLinkId")
        seen.append(base or link)
        return response(
            {"category": "option", "list": [execution(base or link, category="option")]}
        )

    async with adapter(handle) as instance:
        assert len(await instance.get_executions("option")) == 2
        assert len(await instance.get_executions("option", order_link_ids=["a", "b", "a"])) == 2
    assert seen == ["BTC", "ETH", "a", "b"]


async def test_offline_execution_windows_split_and_complete_before_return():
    end = now_utc() - dt.timedelta(seconds=1)
    start = end - dt.timedelta(days=15)
    windows = []

    def handle(req):
        a, b = int(req.url.params["startTime"]), int(req.url.params["endTime"])
        windows.append((a, b))
        assert b - a < 7 * 24 * 3600 * 1000
        return response({"list": [execution(str(a), stamp=a)]})

    async with adapter(handle) as instance:
        rows = await instance.get_executions("spot", start_time=start, end_time=end)
    assert len(rows) == len(windows) == 3
    assert windows[0][0] == int(start.timestamp() * 1000)
    assert windows[-1][1] == int(end.timestamp() * 1000)
    assert windows[1][0] == windows[0][1] + 1


async def test_later_recovery_window_failure_never_returns_partial_rows():
    calls = 0
    end = now_utc() - dt.timedelta(seconds=1)

    def handle(req):
        nonlocal calls
        calls += 1
        if calls == 1:
            stamp = int(req.url.params["startTime"])
            return response({"list": [execution("first", stamp=stamp)]})
        return response({}, code=10005)

    async with adapter(handle) as instance:
        with pytest.raises(BybitRestError):
            await instance.get_executions(
                "spot", start_time=end - dt.timedelta(days=8), end_time=end
            )
    assert calls == 2


async def test_unfiltered_option_order_history_enumerates_all_bases():
    bases = []

    def handle(req):
        if req.url.path.endswith("option-base-coins"):
            return response({"list": [{"baseCoin": "BTC"}, {"baseCoin": "ETH"}]})
        base = req.url.params["baseCoin"]
        bases.append(base)
        return response({"list": [order(base, category="option")]})

    async with adapter(handle) as instance:
        assert len(await instance.get_order_history("option")) == 2
    assert bases == ["BTC", "ETH"]


async def test_option_empty_registry_is_not_complete_history():
    async with adapter(lambda req: response({"list": []})) as instance:
        with pytest.raises(InsufficientCapability, match="coverage"):
            await instance.get_executions("option")
        with pytest.raises(InsufficientCapability, match="coverage"):
            await instance.get_order_history("option")


async def test_execution_retention_and_naive_time_rejected():
    async with adapter(lambda req: response({"list": []})) as instance:
        with pytest.raises(InsufficientCapability, match="retained"):
            await instance.get_executions("spot", start_time=now_utc() - dt.timedelta(days=731))
        with pytest.raises(ValueError, match="timezone-aware"):
            await instance.get_executions("spot", start_time=dt.datetime(2026, 1, 1))


async def test_empty_explicit_link_list_never_broadens_query():
    def fail(req):
        raise AssertionError("empty ids must not become all history")

    async with adapter(fail) as instance:
        assert await instance.get_executions("option", order_link_ids=[]) == []
        assert await instance.get_order_history("option", order_link_ids=[]) == []


async def test_history_covers_every_link_and_page():
    seen = []

    def handle(req):
        link, cursor = req.url.params["orderLinkId"], req.url.params.get("cursor")
        seen.append((link, cursor))
        return response({"list": [order(link)], "nextPageCursor": "next" if not cursor else ""})

    async with adapter(handle) as instance:
        assert len(await instance.get_order_history("spot", order_link_ids=["a", "b"])) == 2
    assert seen == [("a", None), ("a", "next"), ("b", None), ("b", "next")]


@pytest.mark.parametrize("mode", ["timeout", "bad-json", "duplicate", "unknown-code", "http500"])
async def test_ambiguous_writes_sent_once(mode):
    calls = 0

    def handle(req):
        nonlocal calls
        calls += 1
        if mode == "timeout":
            raise httpx.ReadTimeout("synthetic timeout")
        if mode == "bad-json":
            return httpx.Response(200, content=b"invalid")
        if mode == "http500":
            return httpx.Response(500)
        return response({}, code=110072 if mode == "duplicate" else 999999)

    async with adapter(handle, attempts=5) as instance:
        with pytest.raises(OrderUncertain):
            await instance.create_order(request())
    assert calls == 1


async def test_explicit_write_rejection_is_not_unknown():
    async with adapter(lambda req: response({}, code=110004)) as instance:
        with pytest.raises(OrderRejected) as caught:
            await instance.create_order(request())
    assert caught.value.ret_code == 110004


@pytest.mark.parametrize(
    "handler_factory",
    [
        lambda: (lambda req: httpx.Response(429, json={"retCode": 10006, "retMsg": "busy"})),
        lambda: (lambda req: response({}, code=10006)),
        lambda: (lambda req: response({}, code=10018)),
    ],
)
async def test_rate_limited_write_is_a_definite_rejection(handler_factory):
    """限流发生在创建订单之前，是"确定未受理"，不是"结果未知"。

    回归来源：写侧把限流也打成 OrderUncertain → UNKNOWN → 账户锁定暂停，
    交易所限频一次就足以让实盘永远无法开仓。
    """
    async with adapter(handler_factory()) as instance:
        with pytest.raises(OrderRejected) as caught:
            await instance.create_order(request())
    assert caught.value.ret_code == 429


async def test_write_waits_out_a_short_rate_limit_cooldown_before_sending():
    calls = 0

    def handle(req):
        nonlocal calls
        calls += 1
        return response({"orderId": "accepted-1", "orderLinkId": "contract-test-1"})

    async with adapter(handle) as instance:
        instance._write_ready_at["/v5/order/create"] = time.monotonic() + 0.05
        result = await instance.create_order(request())
    assert calls == 1 and result.order_id == "accepted-1"


async def test_write_refuses_to_send_during_a_long_rate_limit_cooldown():
    def fail(req):
        raise AssertionError("冷却期内不得发出写请求")

    async with adapter(fail) as instance:
        instance._write_ready_at["/v5/order/create"] = time.monotonic() + 600
        with pytest.raises(OrderRejected):
            await instance.create_order(request())


async def test_open_order_query_asks_only_for_open_orders():
    """openOnly=0 会把历史订单整批拉回来，既触发限流又污染对账。"""
    seen = []

    def handle(req):
        seen.append(req.url.params.get("openOnly"))
        return response({"list": [], "nextPageCursor": ""})

    async with adapter(handle) as instance:
        await instance.get_open_orders("spot")
    assert seen == ["1"]


async def test_created_order_status_is_recognised():
    """V5 条件单会先处于 Created；词表缺它会让私有流解析抛错并断开重连。"""
    assert "Created" in ORDER_STATUSES


async def test_linear_order_uses_one_way_position_and_reduce_only():
    """U 本位下单：一号仓位模式 + reduceOnly，不带现货的 isLeverage。"""

    def handle(req):
        body = json.loads(req.content)
        assert body["category"] == "linear"
        assert body["positionIdx"] == 0
        assert body["reduceOnly"] is True
        assert "isLeverage" not in body
        return response({"orderId": "accepted-1", "orderLinkId": body["orderLinkId"]})

    async with adapter(handle) as instance:
        await instance.create_order(request(category="linear", reduce_only=True))


async def test_linear_instruments_query_omits_the_option_only_base_coin():
    """baseCoin=All 只对期权有效；linear 的 instruments-info 不接受该参数。"""
    seen = []

    def handle(req):
        seen.append(dict(req.url.params))
        return response(
            {
                "list": [
                    {
                        "symbol": "BTCUSDT",
                        "baseCoin": "BTC",
                        "quoteCoin": "USDT",
                        "settleCoin": "USDT",
                        "status": "Trading",
                        "contractType": "LinearPerpetual",
                        "priceFilter": {"tickSize": "0.01"},
                        "lotSizeFilter": {
                            "qtyStep": "0.001",
                            "minOrderQty": "0.001",
                            "maxOrderQty": "100",
                        },
                    }
                ],
                "nextPageCursor": "",
            }
        )

    async with adapter(handle) as instance:
        specs = await instance.get_instruments("linear")
    assert seen and "baseCoin" not in seen[0]
    assert specs[0].symbol == "BTCUSDT"
    assert specs[0].qty_step == D("0.001")
    assert specs[0].tick_size == D("0.01")


async def test_spot_no_leverage_and_signed_json_bytes():
    def handle(req):
        body = json.loads(req.content)
        assert body["isLeverage"] == 0 and body["qty"] == "0.01"
        raw = f"{req.headers['X-BAPI-TIMESTAMP']}synthetic-key5000".encode() + req.content
        assert (
            req.headers["X-BAPI-SIGN"]
            == hmac.new(b"synthetic-secret", raw, hashlib.sha256).hexdigest()
        )
        return response({"orderId": "accepted-1", "orderLinkId": body["orderLinkId"]})

    async with adapter(handle) as instance:
        result = await instance.create_order(request())
    # 回执措辞必须在状态机词表内，否则 effective_status 会归一成 UNKNOWN。
    assert result.status == "New"
    assert (
        effective_status(exchange_status=result.status, qty=D("0.01"), qty_filled=D("0"))
        == ACCEPTED
    )


async def test_option_sale_requires_reduce_only_and_one_way_position():
    def handle(req):
        body = json.loads(req.content)
        assert body["reduceOnly"] is True and body["positionIdx"] == 0
        return response({"orderId": "accepted-1", "orderLinkId": body["orderLinkId"]})

    async with adapter(handle) as instance:
        with pytest.raises(ValueError, match="reduce-only"):
            await instance.create_order(request(category="option", side=OrderSide.SELL))
        await instance.create_order(
            request(category="option", side=OrderSide.SELL, reduce_only=True)
        )


async def test_live_test_guard_blocks_even_explicit_enabled_mock_write():
    def fail(req):
        raise AssertionError("guard must prevent transport call")

    async with adapter(fail, environment="live") as instance:
        with pytest.raises(InsufficientCapability, match="automated tests"):
            await instance.create_order(request())


async def test_paper_guard_blocks_remote_writes():
    async with adapter(lambda req: response({}), environment="paper") as instance:
        with pytest.raises(OrderRejected, match="disabled"):
            await instance.create_order(request())


async def test_cancel_ack_is_only_acceptance():
    async with adapter(lambda req: response({"orderId": "o1", "orderLinkId": "one"})) as instance:
        result = await instance.cancel_order("spot", "BTCUSDT", order_link_id="one")
    assert result.accepted and not hasattr(result, "terminal_confirmed")


async def test_unknown_order_status_and_impossible_quantity_fail_closed():
    for change in ({"orderStatus": "InventedTerminal"}, {"cumExecQty": "2"}):
        async with adapter(
            lambda req, delta=change: response({"list": [{**order(), **delta}]})
        ) as instance:
            with pytest.raises(BybitRestError):
                await instance.get_order_history("spot")


async def test_ack_identity_mismatch_is_uncertain():
    async with adapter(
        lambda req: response({"orderId": "wrong", "orderLinkId": "another"})
    ) as instance:
        with pytest.raises(OrderUncertain, match="identity"):
            await instance.create_order(request())
        with pytest.raises(OrderUncertain, match="identity"):
            await instance.cancel_order("spot", "BTCUSDT", order_id="expected")


async def test_read_permission_failure_is_not_retried_and_raw_message_redacted():
    calls = 0

    def handle(req):
        nonlocal calls
        calls += 1
        return httpx.Response(
            200, json={"retCode": 10005, "retMsg": "synthetic-secret", "result": {}, "time": STAMP}
        )

    async with adapter(handle) as instance:
        with pytest.raises(BybitRestError) as caught:
            await instance.get_key_info()
    assert calls == 1 and "synthetic-secret" not in str(caught.value)


async def test_read_retries_bounded_and_limit_headers_recorded():
    calls = 0

    def handle(req):
        nonlocal calls
        calls += 1
        if calls < 3:
            return response(
                {}, code=10006, headers={"X-Bapi-Limit": "10", "X-Bapi-Limit-Status": "0"}
            )
        return response({"timeSecond": "1730000000"})

    async with adapter(handle) as instance:
        instance._sleep_backoff = AsyncMock()
        await instance.get_server_time()
        assert instance._sleep_backoff.await_count == 2
        assert instance.rate_limits["/v5/market/time"]["X-Bapi-Limit"] == 10
    assert calls == 3


async def test_long_server_cooldown_fails_without_retry_storm():
    calls = 0

    def handle(req):
        nonlocal calls
        calls += 1
        return response({}, status=429, headers={"Retry-After": "60"})

    async with adapter(handle) as instance:
        with pytest.raises(RateLimited) as caught:
            await instance.get_server_time()
    assert calls == 1 and caught.value.retry_after >= 60


async def test_transport_read_failure_retries_only_configured_attempts():
    calls = 0

    def handle(req):
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("synthetic")

    async with adapter(handle, attempts=2) as instance:
        instance._sleep_backoff = AsyncMock()
        with pytest.raises(TransientError):
            await instance.get_server_time()
    assert calls == 2


@pytest.mark.parametrize(
    "mutation",
    [
        {"execFee": ""},
        {"feeCurrency": ""},
        {"execQty": "NaN"},
        {"execType": "Delivery"},
    ],
)
async def test_execution_accounting_fields_fail_closed(mutation):
    async with adapter(lambda req: response({"list": [{**execution(), **mutation}]})) as instance:
        with pytest.raises((BybitRestError, InsufficientCapability)):
            await instance.get_executions("spot")


def book_message(*, seq=100, update=20, bids=None, asks=None, age=0):
    return {
        "type": "snapshot",
        "ts": int((now_utc() - dt.timedelta(seconds=age)).timestamp() * 1000),
        "data": {
            "s": "BTCUSDT",
            "u": update,
            "seq": seq,
            "b": [["99", "1"]] if bids is None else bids,
            "a": [["101", "2"]] if asks is None else asks,
        },
    }


def book_stream():
    return BybitOrderBookStream(
        BybitWebSocketClient(url="wss://stream-testnet.bybit.com/v5/public/spot"), category="spot"
    )


def test_ws_seq_and_update_may_jump_without_false_gap():
    stream = book_stream()
    first = stream.apply_snapshot("spot", "BTCUSDT", book_message())
    result = stream.apply_delta(
        "spot", "BTCUSDT", book_message(seq=190, update=40, bids=[["100", "3"]], asks=[])
    )
    assert first.source_ts and result.source_ts
    assert result.bid() == D("100") and not result.gap_detected


@pytest.mark.parametrize(
    "message",
    [
        book_message(seq=99, update=22),
        book_message(seq=101, update=1),
        book_message(seq=101, update=19),
        book_message(seq=101, update=21, age=60),
        book_message(seq=101, update=21, bids=[["NaN", "1"]]),
    ],
)
def test_ws_invalid_delta_requires_fresh_snapshot(message):
    stream = book_stream()
    stream.apply_snapshot("spot", "BTCUSDT", book_message())
    assert stream.apply_delta("spot", "BTCUSDT", message) is None
    assert stream.apply_delta("spot", "BTCUSDT", book_message(seq=200, update=50)) is None
    assert not stream.apply_snapshot(
        "spot", "BTCUSDT", book_message(seq=200, update=50)
    ).gap_detected


def test_ws_no_snapshot_and_reconnect_invalidate_delta():
    stream = book_stream()
    assert stream.apply_delta("spot", "BTCUSDT", book_message()) is None
    stream.apply_snapshot("spot", "BTCUSDT", book_message())
    stream.client.reconnect_count += 1
    assert stream.apply_delta("spot", "BTCUSDT", book_message(seq=101, update=21)) is None


def test_ws_crossed_book_invalidates_without_applying_partial_changes():
    stream = book_stream()
    stream.apply_snapshot("spot", "BTCUSDT", book_message())
    assert (
        stream.apply_delta(
            "spot", "BTCUSDT", book_message(seq=101, update=21, bids=[["102", "1"]], asks=[])
        )
        is None
    )
    assert D("102") not in stream._states["BTCUSDT"].bids


def test_ws_snapshot_zero_levels_filtered_and_repeated_snapshot_allowed():
    stream = book_stream()
    message = book_message(bids=[["98", "0"], ["99", "1"]])
    first = stream.apply_snapshot("spot", "BTCUSDT", message)
    second = stream.apply_snapshot("spot", "BTCUSDT", message)
    assert len(first.bids) == 1 and not second.gap_detected


@pytest.mark.parametrize(
    "message",
    [
        book_message(age=60),
        book_message(bids=[["0", "1"]]),
        book_message(bids=[["101", "1"]]),
        book_message(asks=[]),
    ],
)
def test_ws_invalid_snapshot_cannot_restore_trust(message):
    with pytest.raises(WebSocketError):
        book_stream().apply_snapshot("spot", "BTCUSDT", message)


async def test_private_auth_requires_positive_matching_ack_and_redacts_response():
    client = BybitWebSocketClient(
        url="wss://stream-testnet.bybit.com/v5/private",
        api_key="synthetic-key",
        api_secret="synthetic-secret",
    )
    client.send = AsyncMock()
    client.recv = AsyncMock(
        return_value=json.dumps({"op": "subscribe", "success": True, "echo": "synthetic-key"})
    )
    with pytest.raises(WebSocketError) as caught:
        await client.auth()
    assert not client.authenticated and "synthetic-key" not in str(caught.value)
    client.recv = AsyncMock(return_value='{"op":"auth","success":true}')
    await client.auth()
    assert client.authenticated


async def test_private_events_dedupe_category_and_do_not_lose_other_category():
    stream = BybitPrivateStream(
        BybitWebSocketClient(url="wss://stream-testnet.bybit.com/v5/private")
    )
    stream._running = True
    same = execution("one", exec_id="same")
    option = {**same, "category": "option"}
    await stream._dispatch({"topic": "execution", "data": [same, same, option]})
    spot_iterator, option_iterator = (
        stream.execution_updates("spot"),
        stream.execution_updates("option"),
    )
    assert (await anext(spot_iterator)).category == "spot"
    assert (await anext(option_iterator)).category == "option"
    assert stream._execution_queues["spot"].empty()
    await spot_iterator.aclose()
    await option_iterator.aclose()
    assert stream.needs_reconciliation


async def test_private_disconnect_invalidates_before_backoff(monkeypatch):
    client = BybitWebSocketClient(url="wss://stream-testnet.bybit.com/v5/private")
    client.connect = AsyncMock()
    client.auth = AsyncMock()
    client._authenticated = True
    client.subscribe = AsyncMock()
    client.disconnect = AsyncMock()
    stream = BybitPrivateStream(client)
    stream._running = True
    stream._consume = AsyncMock(side_effect=RuntimeError("synthetic-secret"))

    async def check_sleep(delay):
        assert not stream.connected and stream.needs_reconciliation
        stream._running = False

    monkeypatch.setattr("bybitbot.adapters.bybit_ws.asyncio.sleep", check_sleep)
    await stream._run()
    assert client.disconnect.await_count == 1


async def test_private_invalid_execution_does_not_enter_dedupe_cache():
    stream = BybitPrivateStream(
        BybitWebSocketClient(url="wss://stream-testnet.bybit.com/v5/private")
    )
    item = {**execution(), "execQty": "NaN"}
    with pytest.raises(WebSocketError):
        await stream._dispatch({"topic": "execution", "data": [item]})
    assert not stream._seen_exec_ids and stream.needs_reconciliation
