"""Public endpoint paging and HV parsing regressions; no network or credentials."""

from __future__ import annotations

import pathlib
import sys

import httpx

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from fetch_bybit_data import fetch_hv, fetch_klines  # noqa: E402


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
