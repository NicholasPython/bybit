"""Synthetic public snapshot checks; no network, credentials or old study mutations."""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import importlib.util
import json
import subprocess
import urllib.parse
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

MODULE_PATH = Path(__file__).with_name("market.py")
SPEC = importlib.util.spec_from_file_location("bull_call_market_test_subject", MODULE_PATH)
market = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(market)
D = Decimal
CUTOFF = int(dt.datetime(2026, 11, 15, 10, 59, 58, tzinfo=dt.UTC).timestamp()) * 1000


def utc(ms):
    return dt.datetime.fromtimestamp(ms / 1000, dt.UTC).isoformat()


def symbol(base, date="20NOV26", strike="100000"):
    return f"{base}-{date}-{strike}-C-USDT"


def instrument(base, date="20NOV26", strike="100000"):
    expiry = dt.datetime.strptime(date, "%d%b%y").replace(hour=8, tzinfo=dt.UTC)
    return {
        "symbol": symbol(base, date, strike),
        "baseCoin": base,
        "optionsType": "Call",
        "quoteCoin": "USDT",
        "settleCoin": "USDT",
        "status": "Trading",
        "deliveryTime": str(int(expiry.timestamp()) * 1000),
        "priceFilter": {"tickSize": "5", "minPrice": "5", "maxPrice": "1000000"},
        "lotSizeFilter": {"minOrderQty": "0.01", "qtyStep": "0.01", "maxOrderQty": "500"},
    }


def ticker(base, date="20NOV26", strike="100000"):
    return {
        "symbol": symbol(base, date, strike),
        "delta": "0.55",
        "indexPrice": "95000",
        "bid1Price": "1000",
        "ask1Price": "1010",
        "bid1Size": "2",
        "ask1Size": "3",
        "time": "1",  # Per-quote fields must not override the response source time.
    }


def candles(width, count):
    boundary = CUTOFF // width * width
    rows = []
    for i in range(count):
        close = 100 + i if width == market.DAY else 200 + i
        rows.append(
            [
                str(boundary - (count - i) * width),
                str(close),
                str(close + 1),
                str(close - 1),
                str(close),
                "1",
                "100",
            ]
        )
    # Unfinished candles may have an unusable close; they must never affect signals.
    rows.append([str(boundary), "NaN", "NaN", "NaN", "NaN", "-1", "-1"])
    return list(reversed(rows))


def snapshot():
    records = []

    def add(name, params, result):
        source = CUTOFF + len(records) * 500
        records.append(
            {
                "path": "/v5/market/" + name,
                "params": params,
                "startedUtc": utc(source),
                "receivedUtc": utc(source + 100),
                "payload": {"retCode": 0, "time": source, "result": result},
            }
        )

    add("time", {}, {"timeSecond": str(CUTOFF // 1000)})
    for base in ("BTC", "ETH"):
        add(
            "instruments-info",
            {"category": "option", "baseCoin": base, "limit": 1000},
            {
                "category": "option",
                "list": [instrument(base)],
                "nextPageCursor": "",
            },
        )
        add(
            "tickers",
            {"category": "option", "baseCoin": base},
            {
                "category": "option",
                "list": [ticker(base)],
            },
        )
        for interval, width, limit, count in (
            ("60", market.HOUR, 1000, 25),
            ("D", market.DAY, 100, 51),
        ):
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
    return {
        "startedUtc": utc(CUTOFF - 100),
        "completedUtc": utc(CUTOFF + 5000),
        "asOfMs": CUTOFF,
        "records": records,
    }


def write_snapshot(tmp_path, data):
    root = tmp_path / "snapshot"
    root.mkdir(exist_ok=True)
    manifest = {k: v for k, v in data.items() if k != "records"}
    manifest["records"] = []
    for i, record in enumerate(data["records"]):
        folder = root / f"request-{i}"
        folder.mkdir(exist_ok=True)
        raw = json.dumps(record["payload"], separators=(",", ":")).encode()
        path = folder / "response.json"
        path.write_bytes(raw)
        manifest["records"].append(
            {
                **{k: v for k, v in record.items() if k != "payload"},
                "file": str(path.relative_to(root)),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
    dest = root / "manifest.json"
    dest.write_text(json.dumps(manifest))
    return dest


def find(data, suffix, base="BTC", interval=None):
    return next(
        r
        for r in data["records"]
        if r["path"].endswith(suffix)
        and (r["params"].get("baseCoin") == base or r["params"].get("symbol") == base + "USDT")
        and (interval is None or r["params"].get("interval") == interval)
    )


def set_closes(rows, values):
    completed = sorted(rows[1:], key=lambda row: int(row[0]))
    assert len(completed) == len(values)
    for row, value in zip(completed, values, strict=True):
        row[1:5] = [str(value), str(value + 1), str(value - 1), str(value)]


def test_interface_and_common_completed_boundaries(tmp_path):
    data = snapshot()
    path = write_snapshot(tmp_path, data)
    market.verify_snapshot(path)
    result = market.load_market(path)
    assert set(result) == {"ms", "signals", "options"}
    assert result["ms"] == max(r["payload"]["time"] for r in data["records"])
    for base in ("BTC", "ETH"):
        signal = result["signals"][base]
        assert signal == {
            "eligible": True,
            "hourClose": D(224),
            "dailyClose": D(150),
            "sma20": D("140.5"),
            "previousSma20": D("139.5"),
            "sma50": D("125.5"),
            "return24h": D(".12"),
            "last3HourlyCloses": [D(222), D(223), D(224)],
            "lastHourMs": CUTOFF // market.HOUR * market.HOUR,
            "lastDayMs": CUTOFF // market.DAY * market.DAY,
        }
        option = result["options"][symbol(base)]
        assert option["sourceMs"] == find(data, "/tickers", base)["payload"]["time"]
        assert type(option["expiryMs"]) is int
        assert type(option["sourceMs"]) is int
        for key in (
            "strike",
            "delta",
            "index",
            "bid",
            "ask",
            "bidSize",
            "askSize",
            "tick",
            "minQty",
            "step",
            "maxQty",
            "minPrice",
            "maxPrice",
        ):
            assert isinstance(option[key], D)
        # Five-day expiry is kept for exits; no entry DTE filter here.
        assert option["expiryMs"] - CUTOFF < 10 * market.DAY


@pytest.mark.parametrize(
    "gate", ["daily", "previous", "hour1", "hour2", "hour3", "sma50", "return"]
)
def test_each_exact_strong_gate_is_required(tmp_path, gate):
    data = snapshot()
    days = find(data, "/kline", interval="D")["payload"]["result"]["list"]
    hours = find(data, "/kline", interval="60")["payload"]["result"]["list"]
    if gate == "daily":
        # Daily close equals SMA20: strict inequality must fail.
        set_closes(days, [100] * 51)
    elif gate == "previous":
        # Last day above SMA20, but outgoing day is greater: SMA20 declines.
        set_closes(days, [100] * 30 + [200] + [100] * 19 + [120])
    elif gate.startswith("hour"):
        values = list(range(200, 225))
        values[-int(gate[-1])] = D("140.5")
        set_closes(hours, values)
    elif gate == "sma50":
        set_closes(days, [300] * 30 + [100] * 20 + [130])
        set_closes(hours, list(range(106, 131)))
    else:
        set_closes(hours, [224] + list(range(201, 225)))
    result = market.load_market(write_snapshot(tmp_path, data))
    assert result["signals"]["BTC"]["eligible"] is False
    assert result["signals"]["ETH"]["eligible"] is True


def test_flat_sma20_is_allowed(tmp_path):
    data = snapshot()
    days = find(data, "/kline", interval="D")["payload"]["result"]["list"]
    set_closes(days, [100] * 30 + [120] + [100] * 19 + [120])
    signal = market.load_market(write_snapshot(tmp_path, data))["signals"]["BTC"]
    assert signal["sma20"] == signal["previousSma20"]
    assert signal["eligible"] is True


@pytest.mark.parametrize(
    "kind",
    [
        "future",
        "receive_stale",
        "completion_stale",
        "elapsed",
        "snapshot_duration",
        "retcode",
        "negative_elapsed",
        "cutoff",
    ],
)
def test_freshness_rejects_invalid_capture(tmp_path, kind):
    data = snapshot()
    row = data["records"][1]
    if kind == "future":
        row["payload"]["time"] += 101
    elif kind == "receive_stale":
        row["payload"]["time"] -= 30_001
    elif kind == "completion_stale":
        row["payload"]["time"] = CUTOFF - 25_001
    elif kind == "elapsed":
        row["startedUtc"] = utc(CUTOFF - 30_001)
    elif kind == "snapshot_duration":
        data["completedUtc"] = utc(CUTOFF + 30_001)
    elif kind == "retcode":
        row["payload"]["retCode"] = 10001
    elif kind == "negative_elapsed":
        row["startedUtc"] = utc(CUTOFF + 601)
    else:
        data["asOfMs"] += 1
    with pytest.raises(ValueError):
        market.load_market(write_snapshot(tmp_path, data))


def test_custom_freshness_and_historical_replay(tmp_path):
    path = write_snapshot(tmp_path, snapshot())
    market.load_market(path, max_age_seconds=5)
    with pytest.raises(ValueError, match="Stale"):
        market.load_market(path, max_age_seconds=4)
    for bound in (-1, 31, "NaN"):
        with pytest.raises(ValueError):
            market.load_market(path, max_age_seconds=bound)


@pytest.mark.parametrize(
    "interval,kind",
    [
        (i, k)
        for i in ("60", "D")
        for k in (
            "gap",
            "duplicate",
            "alignment",
            "nan",
            "infinity",
            "zero",
            "order",
            "negative_volume",
            "negative_turnover",
            "short_history",
            "stale_boundary",
            "wrong_cutoff",
        )
    ],
)
def test_completed_ohlc_and_continuity(tmp_path, interval, kind):
    data = snapshot()
    record = find(data, "/kline", interval=interval)
    if kind in ("gap", "stale_boundary"):
        width, count = (market.HOUR, 26) if interval == "60" else (market.DAY, 52)
        record["payload"]["result"]["list"] = candles(width, count)
    rows = record["payload"]["result"]["list"]
    if kind == "gap":
        rows.pop(5)
    elif kind == "duplicate":
        rows.append(copy.deepcopy(rows[5]))
    elif kind == "alignment":
        rows[5][0] = str(int(rows[5][0]) + 1)
    elif kind == "nan":
        rows[5][4] = "NaN"
    elif kind == "infinity":
        rows[5][2] = "Infinity"
    elif kind == "zero":
        rows[5][3] = "0"
    elif kind == "order":
        rows[5][2] = "1"
    elif kind == "negative_volume":
        rows[5][5] = "-1"
    elif kind == "negative_turnover":
        rows[5][6] = "-1"
    elif kind == "short_history":
        rows.pop()
    elif kind == "stale_boundary":
        rows.pop(1)
    else:
        record["params"]["end"] += 1
    with pytest.raises((ValueError, ArithmeticError)):
        market.load_market(write_snapshot(tmp_path, data))


@pytest.mark.parametrize(
    "field,value",
    [
        ("bid1Price", "0"),
        ("ask1Price", "NaN"),
        ("ask1Price", "995"),
        ("bid1Size", "0"),
        ("ask1Size", "-1"),
        ("delta", "Infinity"),
        ("indexPrice", "0"),
        ("bid1Price", "1001"),
        ("ask1Price", None),
    ],
)
def test_bad_quote_never_becomes_executable(tmp_path, field, value):
    data = snapshot()
    find(data, "/tickers")["payload"]["result"]["list"][0][field] = value
    result = market.load_market(write_snapshot(tmp_path, data))
    option = result["options"][symbol("BTC")]
    assert all(option[k] is None for k in market.QUOTE_FIELDS)
    assert result["options"][symbol("ETH")]["bid"] > 0


def test_absent_quote_and_nontrading_status(tmp_path):
    data = snapshot()
    find(data, "/tickers")["payload"]["result"]["list"] = []
    find(data, "/instruments-info")["payload"]["result"]["list"][0]["status"] = "Settling"
    option = market.load_market(write_snapshot(tmp_path, data))["options"][symbol("BTC")]
    assert option["status"] == "Settling"
    assert option["bid"] is None and option["ask"] is None


@pytest.mark.parametrize(
    "kind",
    [
        "put",
        "foreign",
        "expiry",
        "strike",
        "filter",
        "missing_max",
        "nan_filter",
        "quote_coin",
        "settle_coin",
    ],
)
def test_malformed_instrument_skipped_per_symbol(tmp_path, kind):
    data = snapshot()
    row = find(data, "/instruments-info")["payload"]["result"]["list"][0]
    if kind == "put":
        row["symbol"] = row["symbol"].replace("-C-", "-P-")
    elif kind == "foreign":
        row["baseCoin"] = "SOL"
    elif kind == "expiry":
        row["deliveryTime"] = str(int(row["deliveryTime"]) + 1)
    elif kind == "strike":
        row["strikePrice"] = "99999"
    elif kind == "filter":
        row["lotSizeFilter"]["qtyStep"] = "0"
    elif kind == "missing_max":
        del row["lotSizeFilter"]["maxOrderQty"]
    elif kind == "nan_filter":
        row["priceFilter"]["tickSize"] = "NaN"
    elif kind == "quote_coin":
        row["quoteCoin"] = "USDC"
    else:
        row["settleCoin"] = "USDC"
    result = market.load_market(write_snapshot(tmp_path, data))
    assert symbol("BTC") not in result["options"]
    assert symbol("ETH") in result["options"]


def test_max_limit_order_qty_and_far_expiry(tmp_path):
    data = snapshot()
    row = instrument("BTC", "24SEP27")
    lot = row["lotSizeFilter"]
    lot["maxLimitOrderQty"] = lot.pop("maxOrderQty")
    find(data, "/instruments-info")["payload"]["result"]["list"] = [row]
    find(data, "/tickers")["payload"]["result"]["list"] = [ticker("BTC", "24SEP27")]
    option = market.load_market(write_snapshot(tmp_path, data))["options"][row["symbol"]]
    assert option["maxQty"] == D(500)
    assert option["expiryMs"] - CUTOFF > 40 * market.DAY


@pytest.mark.parametrize("kind", ["tamper", "absolute", "parent", "symlink", "duplicate_path"])
def test_raw_integrity_and_path_confinement(tmp_path, kind):
    path = write_snapshot(tmp_path, snapshot())
    manifest = json.loads(path.read_bytes())
    record = manifest["records"][0]
    raw = path.parent / record["file"]
    if kind == "tamper":
        raw.write_bytes(raw.read_bytes() + b" ")
    elif kind == "absolute":
        record["file"] = str(raw)
    elif kind == "parent":
        record["file"] = "../snapshot/" + record["file"]
    elif kind == "symlink":
        outside = tmp_path / "outside.json"
        outside.write_bytes(raw.read_bytes())
        raw.unlink()
        raw.symlink_to(outside)
    else:
        manifest["records"][1]["file"] = record["file"]
        manifest["records"][1]["sha256"] = record["sha256"]
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        market.verify_snapshot(path)
    with pytest.raises(ValueError):
        market.load_market(path)


def split_page(data):
    first = find(data, "/instruments-info")
    first["payload"]["result"]["nextPageCursor"] = "page-2"
    second = copy.deepcopy(first)
    second["params"]["cursor"] = "page-2"
    second["payload"]["result"] = {
        "category": "option",
        "list": [instrument("BTC", strike="110000")],
        "nextPageCursor": "",
    }
    # Sequential timestamps can be shared at their receive/start boundary.
    second["startedUtc"] = first["receivedUtc"]
    data["records"].insert(data["records"].index(first) + 1, second)
    return first, second


def test_complete_metadata_pagination(tmp_path):
    data = snapshot()
    split_page(data)
    result = market.load_market(write_snapshot(tmp_path, data))
    assert symbol("BTC", strike="110000") in result["options"]
    assert result["options"][symbol("BTC", strike="110000")]["ask"] is None


@pytest.mark.parametrize(
    "kind",
    [
        "missing_page",
        "bad_cursor",
        "cycle",
        "duplicate_symbol",
        "extra_page",
        "ticker_duplicate",
        "response_duplicate",
    ],
)
def test_bad_pagination_and_duplicates_rejected(tmp_path, kind):
    data = snapshot()
    first, second = split_page(data)
    if kind == "missing_page":
        data["records"].remove(second)
    elif kind == "bad_cursor":
        second["params"]["cursor"] = "wrong"
    elif kind == "cycle":
        second["payload"]["result"]["nextPageCursor"] = "page-2"
    elif kind == "duplicate_symbol":
        second["payload"]["result"]["list"] = copy.deepcopy(first["payload"]["result"]["list"])
    elif kind == "extra_page":
        first["payload"]["result"]["nextPageCursor"] = ""
    elif kind == "ticker_duplicate":
        rows = find(data, "/tickers")["payload"]["result"]["list"]
        rows.append(copy.deepcopy(rows[0]))
    else:
        record = copy.deepcopy(find(data, "/tickers"))
        record["startedUtc"] = record["receivedUtc"]
        data["records"].insert(data["records"].index(find(data, "/tickers")) + 1, record)
    with pytest.raises(ValueError):
        market.load_market(write_snapshot(tmp_path, data))


@pytest.mark.parametrize("kind", ["endpoint", "category", "base", "symbol", "extra_param"])
def test_request_allowlist_rechecked_when_loading(tmp_path, kind):
    data = snapshot()
    row = find(data, "/tickers")
    if kind == "endpoint":
        row["path"] = "/v5/order/create"
    elif kind == "category":
        row["params"]["category"] = "linear"
    elif kind == "base":
        row["params"]["baseCoin"] = "SOL"
    elif kind == "symbol":
        find(data, "/kline", interval="60")["params"]["symbol"] = "SOLUSDT"
    else:
        row["params"]["api_key"] = "unused"
    with pytest.raises(ValueError):
        market.load_market(write_snapshot(tmp_path, data))


def test_collection_uses_local_curl_isolated_raw_and_paginated_metadata(tmp_path, monkeypatch):
    data = snapshot()
    split_page(data)
    expected = iter(data["records"])
    clock = iter(
        [utc(CUTOFF - 100)]
        + [
            stamp
            for row in data["records"]
            for stamp in (row["startedUtc"], row["startedUtc"], row["receivedUtc"])
        ]
        + [utc(CUTOFF + 5000)]
    )
    monkeypatch.setattr(market, "_utc", lambda: next(clock))
    calls = []

    def fake_run(command, **kwargs):
        record = next(expected)
        assert command[:10] == [
            "/usr/bin/curl",
            "-q",
            "--noproxy",
            "*",
            "--max-time",
            "25",
            "--fail",
            "--silent",
            "--show-error",
            command[-1],
        ]
        assert kwargs == {"capture_output": True, "check": True, "timeout": 30}
        url = urllib.parse.urlsplit(command[-1])
        assert url.scheme == "https" and url.netloc == "api.bybit.com"
        assert url.path == record["path"]
        params = dict(urllib.parse.parse_qsl(url.query))
        assert params == {k: str(v) for k, v in record["params"].items()}
        calls.append(command)
        return SimpleNamespace(stdout=json.dumps(record["payload"]).encode())

    monkeypatch.setattr(market.subprocess, "run", fake_run)
    path = market.collect(tmp_path / "captures")
    assert path.is_file() and len(calls) == 10
    manifest = json.loads(path.read_bytes())
    folders = [(path.parent / record["file"]).parent for record in manifest["records"]]
    assert len(set(folders)) == len(folders)
    assert all(sorted(p.name for p in folder.iterdir()) == ["response.json"] for folder in folders)
    market.load_market(path)


def test_exact_freshness_boundaries(tmp_path):
    data = snapshot()
    data["startedUtc"] = utc(CUTOFF)
    data["completedUtc"] = utc(CUTOFF + 30_000)
    for row in data["records"]:
        row["startedUtc"] = utc(CUTOFF)
        row["receivedUtc"] = utc(CUTOFF)
        row["payload"]["time"] = CUTOFF
    data["records"][-1]["receivedUtc"] = utc(CUTOFF + 30_000)
    market.load_market(write_snapshot(tmp_path, data))
    # Zero age is also valid when all records complete at exactly the source time.
    data["completedUtc"] = utc(CUTOFF)
    data["records"][-1]["receivedUtc"] = utc(CUTOFF)
    market.load_market(write_snapshot(tmp_path, data), max_age_seconds=0)


@pytest.mark.parametrize("kind", ["lot_list", "price_list", "bad_decimal", "huge_quote"])
def test_malformed_per_symbol_does_not_crash_same_base_universe(tmp_path, kind):
    data = snapshot()
    rows = find(data, "/instruments-info")["payload"]["result"]["list"]
    quotes = find(data, "/tickers")["payload"]["result"]["list"]
    bad = instrument("BTC", strike="110000")
    bad_quote = ticker("BTC", strike="110000")
    if kind == "lot_list":
        bad["lotSizeFilter"] = []
    elif kind == "price_list":
        bad["priceFilter"] = []
    elif kind == "bad_decimal":
        bad["lotSizeFilter"]["qtyStep"] = "garbage"
    else:
        bad_quote["ask1Price"] = "1e999999"
    rows.append(bad)
    quotes.append(bad_quote)
    result = market.load_market(write_snapshot(tmp_path, data))
    assert result["options"][symbol("BTC")]["bid"] == D(1000)
    if kind == "huge_quote":
        assert result["options"][bad["symbol"]]["ask"] is None
    else:
        assert bad["symbol"] not in result["options"]


@pytest.mark.parametrize("suffix", ["/instruments-info", "/tickers"])
def test_duplicate_symbol_in_single_response_rejected(tmp_path, suffix):
    data = snapshot()
    rows = find(data, suffix)["payload"]["result"]["list"]
    rows.append(copy.deepcopy(rows[0]))
    with pytest.raises(ValueError, match="Duplicate"):
        market.load_market(write_snapshot(tmp_path, data))


def test_reader_can_be_loaded_under_isolated_python_from_other_directory(tmp_path):
    path = write_snapshot(tmp_path, snapshot())
    code = (
        "import runpy,sys; module=runpy.run_path(sys.argv[1]); "
        "module['load_market'](__import__('pathlib').Path(sys.argv[2]))"
    )
    result = subprocess.run(  # noqa: S603 -- test-owned code and fixed local interpreter
        [
            str(Path(__file__).resolve().parents[2] / ".venv/bin/python"),
            "-I",
            "-c",
            code,
            str(MODULE_PATH),
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
