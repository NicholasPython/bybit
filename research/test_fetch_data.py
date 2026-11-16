"""Public endpoint paging, HV parsing and capital measurement regressions.

No network and no credentials are used.
"""

from __future__ import annotations

import datetime as dt
import pathlib
import sys

import httpx
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from fetch_bybit_data import fetch_hv, fetch_klines  # noqa: E402
from option_capital import option_snapshot, spot_snapshot  # noqa: E402


def _delivery_in(days: int) -> str:
    moment = dt.datetime.now(dt.UTC) + dt.timedelta(days=days)
    return str(int(moment.timestamp() * 1000))


def test_klines_page_backwards_to_oldest_requested_bar() -> None:
    ends = []

    def respond(request: httpx.Request) -> httpx.Response:
        end = int(request.url.params["end"])
        ends.append(end)
        stamps = [s for s in (5000, 4000, 3000, 2000, 1000) if 1000 <= s <= end][:2]
        return httpx.Response(
            200,
            json={
                "retCode": 0,
                "result": {"list": [[str(s), "1", "1", "1", "1", "1", "1"] for s in stamps]},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        rows = fetch_klines(client, "BTCUSDT", "D", 1000, 5000, limit=2)
    assert [int(row[0]) for row in rows] == [1000, 2000, 3000, 4000, 5000]
    assert ends == [5000, 3999, 1999]


def test_hv_uses_usdt_and_parses_object_array() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.params["quoteCoin"] == "USDT"
        return httpx.Response(
            200,
            json={
                "retCode": 0,
                "result": [
                    {"period": 7, "time": "2000", "value": "0.3"},
                    {"period": 7, "time": "1000", "value": "0.2"},
                ],
            },
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        assert fetch_hv(client, "BTC", 7, 1000, 2000) == [
            ["1000", "0.2", "7"],
            ["2000", "0.3", "7"],
        ]


def test_spot_capital_uses_minimum_order_amount() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("instruments-info"):
            return httpx.Response(
                200,
                json={
                    "retCode": 0,
                    "result": {
                        "nextPageCursor": "",
                        "list": [
                            {
                                "symbol": "BTCUSDT",
                                "quoteCoin": "USDT",
                                "status": "Trading",
                                "lotSizeFilter": {
                                    "minOrderAmt": "5",
                                    "minOrderQty": "0.000001",
                                },
                            },
                            {
                                "symbol": "BMTUSDT",
                                "quoteCoin": "USDT",
                                "status": "Trading",
                                "lotSizeFilter": {"minOrderAmt": "1", "minOrderQty": "1"},
                            },
                            {
                                "symbol": "ETHUSDT",
                                "quoteCoin": "USDT",
                                "status": "Trading",
                                "lotSizeFilter": {
                                    "minOrderAmt": "5",
                                    "minOrderQty": "0.00001",
                                },
                            },
                        ],
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                "retCode": 0,
                "result": {
                    "list": [
                        {"symbol": "BTCUSDT", "lastPrice": "78000", "turnover24h": "1e9"},
                        {"symbol": "BMTUSDT", "lastPrice": "0.02", "turnover24h": "3e5"},
                        {"symbol": "ETHUSDT", "lastPrice": "2300", "turnover24h": "5e8"},
                    ]
                },
            },
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        snapshot = spot_snapshot(client)
    assert snapshot["trading_usdt_pairs"] == 3
    assert snapshot["min_order_amt_distribution"] == {"1.0": 1, "5.0": 2}
    assert [row["symbol"] for row in snapshot["pairs_below_5_usdt"]] == ["BMTUSDT"]
    assert snapshot["cheapest_by_cash"][0]["min_cash"] == 1.0


def test_option_capital_uses_ask_side_and_drops_one_sided_quotes() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        is_instruments = request.url.path.endswith("instruments-info")
        if params.get("category") == "option" and is_instruments:
            return httpx.Response(
                200,
                json={
                    "retCode": 0,
                    "result": {
                        "nextPageCursor": "",
                        "list": [
                            {
                                "symbol": "ETH-27NOV26-2400-C-USDT",
                                "status": "Trading",
                                "optionsType": "Call",
                                "deliveryTime": _delivery_in(25),
                                "deliveryFeeRate": "0.00015",
                                "lotSizeFilter": {"minOrderQty": "0.1"},
                            },
                            {
                                "symbol": "ETH-27NOV26-3000-C-USDT",
                                "status": "Trading",
                                "optionsType": "Call",
                                "deliveryTime": _delivery_in(25),
                                "deliveryFeeRate": "0.00015",
                                "lotSizeFilter": {"minOrderQty": "0.1"},
                            },
                        ],
                    },
                },
            )
        if params.get("category") == "option":
            return httpx.Response(
                200,
                json={
                    "retCode": 0,
                    "result": {
                        "list": [
                            {
                                "symbol": "ETH-27NOV26-2400-C-USDT",
                                "bid1Price": "78.9",
                                "ask1Price": "79.3",
                                "iv": "0.6",
                            },
                            {
                                "symbol": "ETH-27NOV26-3000-C-USDT",
                                "bid1Price": "0",
                                "ask1Price": "0.1",
                                "iv": "0",
                            },
                        ]
                    },
                },
            )
        return httpx.Response(
            200,
            json={"retCode": 0, "result": {"list": [{"lastPrice": "2293.29"}]}},
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        snapshot = option_snapshot(client, "ETH")
    assert snapshot["two_sided_contracts"] == 1
    assert snapshot["min_order_qty"] == 0.1
    assert snapshot["min_premium_usdt"]["min"] == pytest.approx(7.93)
    # min(index x 0.0003, premium x 0.07) x qty, capped by the index leg.
    assert snapshot["cheapest_two_sided"][0]["taker_fee"] == pytest.approx(0.0687987)
    assert [row["symbol"] for row in snapshot["near_atm_20_45d"]] == [
        "ETH-27NOV26-2400-C-USDT"
    ]
