"""Synthetic public snapshot integrity; no network or account access."""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json

import pytest

from bybitbot.marketdata import DAY, HOUR, load_snapshot, verify_snapshot

CUTOFF = int(dt.datetime(2026, 11, 15, 12, tzinfo=dt.UTC).timestamp() * 1000)


def utc(ms):
    return dt.datetime.fromtimestamp(ms / 1000, dt.UTC).isoformat()


def spec(base):
    return {
        "symbol": f"{base}-27NOV26-1000-C-USDT",
        "baseCoin": base,
        "quoteCoin": "USDT",
        "settleCoin": "USDT",
        "status": "Trading",
        "optionsType": "Call",
        "deliveryTime": str(int(dt.datetime(2026, 11, 27, 8, tzinfo=dt.UTC).timestamp() * 1000)),
        "strikePrice": "1000",
        "priceFilter": {"minPrice": "0.1", "maxPrice": "10000", "tickSize": "0.1"},
        "lotSizeFilter": {"minOrderQty": "0.01", "qtyStep": "0.01", "maxOrderQty": "100"},
    }


def candles(width, count):
    boundary = CUTOFF // width * width
    return [
        [str(boundary - i * width), "100", "102", "99", "101", "1", "100"]
        for i in range(count, 0, -1)
    ]


def snapshot(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    manifest = {
        "startedUtc": utc(CUTOFF),
        "completedUtc": utc(CUTOFF + 1000),
        "asOfMs": CUTOFF,
        "records": [],
    }

    def add(endpoint, params, result):
        number = len(manifest["records"])
        folder = root / f"request-{number}"
        folder.mkdir()
        raw = json.dumps({"retCode": 0, "time": CUTOFF, "result": result}).encode()
        (folder / "response.json").write_bytes(raw)
        manifest["records"].append(
            {
                "path": f"/v5/market/{endpoint}",
                "params": params,
                "startedUtc": utc(CUTOFF + number * 20),
                "receivedUtc": utc(CUTOFF + number * 20 + 10),
                "file": f"request-{number}/response.json",
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        )

    add("time", {}, {})
    for base in ("BTC", "ETH"):
        instrument = spec(base)
        add(
            "instruments-info",
            {"category": "option", "baseCoin": base, "limit": 1000},
            {
                "category": "option",
                "list": [instrument],
                "nextPageCursor": "",
            },
        )
        add(
            "tickers",
            {"category": "option", "baseCoin": base},
            {
                "category": "option",
                "list": [
                    {
                        "symbol": instrument["symbol"],
                        "delta": "0.55",
                        "indexPrice": "1000",
                        "bid1Price": "29.9",
                        "ask1Price": "30",
                        "bid1Size": "10",
                        "ask1Size": "10",
                    }
                ],
            },
        )
        for interval, limit, width, count in (("60", 1000, HOUR, 25), ("D", 100, DAY, 51)):
            add(
                "kline",
                {
                    "category": "spot",
                    "symbol": base + "USDT",
                    "interval": interval,
                    "limit": limit,
                    "end": CUTOFF,
                },
                {"category": "spot", "symbol": base + "USDT", "list": candles(width, count)},
            )
    path = root / "manifest.json"
    path.write_text(json.dumps(manifest))
    return path


def rewrite_record(path, number, mutate):
    manifest = json.loads(path.read_text())
    record = manifest["records"][number]
    raw_path = path.parent / record["file"]
    payload = json.loads(raw_path.read_text())
    mutate(payload)
    raw = json.dumps(payload).encode()
    raw_path.write_bytes(raw)
    record["sha256"] = hashlib.sha256(raw).hexdigest()
    path.write_text(json.dumps(manifest))


def load(path):
    return load_snapshot(path, minimum_hours=25, minimum_days=51)


def test_synthetic_snapshot_has_complete_history_and_shared_specs(tmp_path):
    path = snapshot(tmp_path)
    market = load(path)
    assert market["ms"] == CUTOFF
    assert len(market["options"]) == 2
    assert market["options"]["BTC-27NOV26-1000-C-USDT"]["strike"] == 1000
    assert len(market["candles"]["BTC"]["hours"]) == 25
    assert len(market["candles"]["ETH"]["days"]) == 51
    verify_snapshot(path)


def test_raw_hash_changes_rejected_before_decode(tmp_path):
    path = snapshot(tmp_path)
    (path.parent / "request-0/response.json").write_bytes(b"{}")
    with pytest.raises(ValueError, match="SHA256"):
        load(path)


@pytest.mark.parametrize("kind", ["absolute", "traversal", "symlink", "duplicate"])
def test_raw_paths_cannot_escape_or_reuse_evidence(tmp_path, kind):
    path = snapshot(tmp_path)
    manifest = json.loads(path.read_text())
    if kind == "absolute":
        manifest["records"][0]["file"] = str(path.parent / "request-0/response.json")
    elif kind == "traversal":
        manifest["records"][0]["file"] = "../manifest.json"
    elif kind == "symlink":
        outside = tmp_path / "outside.json"
        outside.write_text("{}")
        link = path.parent / "alias.json"
        link.symlink_to(outside)
        manifest["records"][0]["file"] = "alias.json"
    else:
        manifest["records"][1]["file"] = manifest["records"][0]["file"]
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="path"):
        load(path)


@pytest.mark.parametrize("kind", ["late", "backwards", "cutoff", "endpoint", "missing"])
def test_chronology_cutoff_and_request_set_are_checked(tmp_path, kind):
    path = snapshot(tmp_path)
    manifest = json.loads(path.read_text())
    if kind == "late":
        manifest["completedUtc"] = utc(CUTOFF + 31_000)
    elif kind == "backwards":
        manifest["records"][2]["startedUtc"] = utc(CUTOFF)
    elif kind == "cutoff":
        manifest["asOfMs"] += 1
    elif kind == "endpoint":
        manifest["records"][1]["path"] = "/v5/order/create"
    else:
        del manifest["records"][2]
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        load(path)


@pytest.mark.parametrize("kind", ["cursor", "duplicate", "category", "stale", "retcode"])
def test_response_integrity_after_valid_hash(tmp_path, kind):
    path = snapshot(tmp_path)

    def mutate(payload):
        if kind == "cursor":
            payload["result"].update(nextPageCursor="another")
        elif kind == "duplicate":
            payload["result"]["list"].append(copy.deepcopy(payload["result"]["list"][0]))
        elif kind == "category":
            payload["result"].update(category="linear")
        elif kind == "stale":
            payload.update(time=CUTOFF - 31_000)
        else:
            payload.update(retCode=True)

    rewrite_record(path, 1, mutate)
    with pytest.raises(ValueError):
        load(path)


@pytest.mark.parametrize("kind", ["gap", "duplicate", "ohlc", "volume", "misaligned", "boundary"])
def test_completed_candles_fail_closed(tmp_path, kind):
    path = snapshot(tmp_path)

    def mutate(payload):
        rows = payload["result"]["list"]
        if kind == "gap":
            del rows[10]
        elif kind == "duplicate":
            rows.append(copy.deepcopy(rows[-1]))
        elif kind == "ohlc":
            rows[-1][2] = "98"
        elif kind == "volume":
            rows[-1][5] = "-1"
        elif kind == "misaligned":
            rows[-1][0] = str(int(rows[-1][0]) - 1)
        else:
            # Enough rows remain; last completed boundary is stale.
            rows.insert(0, [str(int(rows[0][0]) - HOUR), *rows[0][1:]])
            rows.pop()

    rewrite_record(path, 3, mutate)
    with pytest.raises(ValueError):
        load(path)


def test_invalid_quote_is_unavailable_not_zero(tmp_path):
    path = snapshot(tmp_path)
    rewrite_record(path, 2, lambda p: p["result"]["list"][0].update(bid1Price="NaN"))
    quote = load(path)["options"]["BTC-27NOV26-1000-C-USDT"]
    assert quote["bid"] is None
    assert quote["ask"] is None
    assert quote["strike"] == 1000
