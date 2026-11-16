"""Public-only BTC/ETH snapshot transport and normalized market observations.

Run readers with ``python -I``. Raw bodies live in separate, newly created request
folders. Freshness is relative to capture completion, so audited snapshots replay.
Missing or malformed quotes remain unavailable (None), never invented prices.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import subprocess
import tempfile
import urllib.parse
from decimal import Decimal
from pathlib import Path

from bybitbot.terms import option_spec

HOUR = 3_600_000
DAY = 24 * HOUR
BASES = ("BTC", "ETH")
ALLOWED = {f"/v5/market/{name}" for name in ("time", "instruments-info", "tickers", "kline")}
QUOTE_FIELDS = ("delta", "index", "bid", "ask", "bidSize", "askSize")


def _utc() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def _stamp(value: str) -> Decimal:
    value = dt.datetime.fromisoformat(value)
    if value.utcoffset() != dt.timedelta(0):
        raise ValueError("Timestamps must be UTC")
    epoch = value - dt.datetime(1970, 1, 1, tzinfo=dt.UTC)
    return Decimal(epoch.days * DAY + epoch.seconds * 1000) + Decimal(epoch.microseconds) / 1000


def _integer(value) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("Expected integer milliseconds")
    result = int(value)
    if result < 0 or str(result) != str(value):
        raise ValueError("Invalid integer")
    return result


def _decimal(value) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError("Missing numeric value")
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("Nonfinite numeric value")
    return result


def _allowed(path: str, params: dict) -> None:
    if path not in ALLOWED or not isinstance(params, dict):
        raise ValueError("Public endpoint outside allowlist")
    if path.endswith("/time"):
        valid = not params
    elif path.endswith("/instruments-info"):
        valid = (
            set(params) <= {"category", "baseCoin", "limit", "cursor"}
            and params.get("category") == "option"
            and params.get("baseCoin") in BASES
            and params.get("limit") == 1000
            and isinstance(params.get("cursor", ""), str)
        )
    elif path.endswith("/tickers"):
        valid = (
            set(params) == {"category", "baseCoin"}
            and params.get("category") == "option"
            and params.get("baseCoin") in BASES
        ) or (
            set(params) == {"category", "symbol"}
            and params.get("category") == "spot"
            and params.get("symbol") in ("BTCUSDT", "ETHUSDT")
        )
    else:
        valid = (
            set(params) == {"category", "symbol", "interval", "limit", "end"}
            and params.get("category") == "spot"
            and params.get("symbol") in ("BTCUSDT", "ETHUSDT")
            and (params.get("interval"), params.get("limit")) in (("60", 1000), ("D", 100))
            and _integer(params["end"]) > 0
        )
    if not valid:
        raise ValueError("Public request parameters outside allowlist")


def _read_snapshot(manifest_path: Path) -> tuple[dict, list[dict]]:
    manifest_path = Path(manifest_path)
    manifest = json.loads(manifest_path.read_bytes())
    root = manifest_path.parent.resolve(strict=True)
    records = []
    seen = set()
    if not isinstance(manifest.get("records"), list) or not manifest["records"]:
        raise ValueError("Missing snapshot records")
    for item in manifest["records"]:
        name = item["file"]
        if not isinstance(name, str) or not name:
            raise ValueError("Invalid raw response path")
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Raw response path escapes snapshot")
        raw_path = (root / relative).resolve(strict=True)
        if not raw_path.is_relative_to(root) or raw_path in seen:
            raise ValueError("Escaped or duplicate raw response path")
        seen.add(raw_path)
        raw = raw_path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != item["sha256"]:
            raise ValueError("Raw response SHA256 mismatch")
        records.append({**item, "payload": json.loads(raw)})
    return manifest, records


def verify_snapshot(manifest_path: Path) -> None:
    """Recheck every relative raw path and its exact SHA256; no network or clock."""
    _read_snapshot(manifest_path)


def _timing(manifest: dict, records: list[dict], max_age_seconds) -> list[int]:
    age = _decimal(max_age_seconds) * 1000
    if not 0 <= age <= 30_000:
        raise ValueError("Freshness bound must be between zero and 30 seconds")
    started, completed = _stamp(manifest["startedUtc"]), _stamp(manifest["completedUtc"])
    if not 0 <= completed - started <= 30_000:
        raise ValueError("Snapshot duration exceeds 30 seconds")
    times = []
    previous_receive = started
    for record in records:
        _allowed(record["path"], record["params"])
        payload = record["payload"]
        if type(payload.get("retCode")) is not int or payload["retCode"] != 0:
            raise ValueError("Public API failure")
        source = _integer(payload["time"])
        sent, received = _stamp(record["startedUtc"]), _stamp(record["receivedUtc"])
        if not started <= previous_receive <= sent <= received <= completed:
            raise ValueError("Invalid request chronology")
        if not 0 <= received - sent <= 30_000:
            raise ValueError("Request duration exceeds 30 seconds")
        if not 0 <= received - source <= age or not 0 <= completed - source <= age:
            raise ValueError("Stale or future public response")
        previous_receive = received
        times.append(source)
    return times


def _rows(record: dict) -> list:
    result = record["payload"]["result"]
    if "category" in result and result["category"] != record["params"].get("category"):
        raise ValueError("Response category mismatch")
    if "symbol" in result and result["symbol"] != record["params"].get("symbol"):
        raise ValueError("Response symbol mismatch")
    rows = result["list"]
    if not isinstance(rows, list):
        raise ValueError("Invalid response list")
    return rows


def _unique_symbols(rows: list) -> dict:
    found = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("symbol"), str):
            continue
        symbol = row["symbol"]
        if symbol in found:
            raise ValueError("Duplicate metadata/ticker symbol")
        found[symbol] = row
    return found


def _catalog(pages: list[dict]) -> dict:
    if not pages:
        raise ValueError("Missing instrument pages")
    cursor = ""
    seen = set()
    all_rows = []
    for page in pages:
        if page["params"].get("cursor", "") != cursor or cursor in seen:
            raise ValueError("Broken instrument cursor chain")
        seen.add(cursor)
        rows = _rows(page)
        cursor = page["payload"]["result"].get("nextPageCursor", "")
        if not isinstance(cursor, str) or (cursor and not rows):
            raise ValueError("Invalid instrument pagination")
        if cursor and cursor in seen:
            raise ValueError("Repeated instrument cursor")
        all_rows.extend(rows)
    if cursor:
        raise ValueError("Incomplete instrument pagination")
    return _unique_symbols(all_rows)


def completed_closes(
    rows: list, cutoff: int, width: int, minimum: int
) -> list[tuple[int, Decimal]]:
    """校验 K 线行，返回**完整** K 线的 ``(毫秒时间戳, 收盘价)``。

    与 :func:`completed_candles` 同一套规则，但输入是 K 线行本身 ——
    在线采样路径不落原始响应文件时也走这里，避免同一口径写两遍。
    """
    cutoff, width, minimum = map(_integer, (cutoff, width, minimum))
    if width <= 0 or minimum <= 0 or not isinstance(rows, list):
        raise ValueError("Candle width/minimum must be positive and rows must be a list")
    completed = []
    for row in rows:
        if not isinstance(row, list) or not row:
            raise ValueError("Malformed candle")
        timestamp = _integer(row[0])
        if timestamp + width > cutoff:
            continue
        if timestamp % width or len(row) != 7:
            raise ValueError("Misaligned or malformed completed candle")
        o, hi, lo, close, volume, turnover = map(_decimal, row[1:])
        if not 0 < lo <= min(o, close) <= max(o, close) <= hi or min(volume, turnover) < 0:
            raise ValueError("Invalid completed OHLC/volume")
        completed.append((timestamp, close))
    completed.sort()
    if len(completed) < minimum:
        raise ValueError("Insufficient completed history")
    if any(b[0] - a[0] != width for a, b in zip(completed, completed[1:], strict=False)):
        raise ValueError("Completed candle gap or duplicate")
    if completed[-1][0] + width != cutoff // width * width:
        raise ValueError("Stale completed candle boundary")
    return completed


def completed_candles(record: dict, cutoff: int, width: int, minimum: int) -> list[tuple]:
    if _integer(record["params"]["end"]) != cutoff:
        raise ValueError("Candle request must use common first-time cutoff")
    return completed_closes(_rows(record), cutoff, width, minimum)


def normalize_instrument(instrument: dict, base: str, option_type="Call") -> dict:
    """Strict shared specification, with the research observation field names."""
    spec = option_spec(instrument, base=base, option_type=option_type)
    return {
        "symbol": spec.symbol,
        "base": spec.base,
        "status": spec.status,
        "expiryMs": spec.expiry_ms,
        "strike": spec.strike,
        "tick": spec.tick,
        "minPrice": spec.min_price,
        "maxPrice": spec.max_price,
        "minQty": spec.min_qty,
        "step": spec.step,
        "maxQty": spec.max_qty,
    }


def normalize_options(
    instruments: dict, ticker_record: dict, base: str, option_type="Call"
) -> dict:
    tickers = _unique_symbols(_rows(ticker_record))
    source = _integer(ticker_record["payload"]["time"])
    options = {}
    for symbol, instrument in instruments.items():
        try:
            normalized = normalize_instrument(instrument, base, option_type)
        except (KeyError, ValueError, TypeError, ArithmeticError):
            continue
        if normalized is None:
            continue
        normalized.update(dict.fromkeys(QUOTE_FIELDS))
        normalized["sourceMs"] = source
        ticker = tickers.get(symbol)
        if ticker is not None:
            try:
                quote = {
                    key: _decimal(ticker[field])
                    for key, field in zip(
                        QUOTE_FIELDS,
                        ("delta", "indexPrice", "bid1Price", "ask1Price", "bid1Size", "ask1Size"),
                        strict=True,
                    )
                }
                if (
                    not (
                        (0 <= quote["delta"] <= 1)
                        if option_type == "Call"
                        else (-1 <= quote["delta"] <= 0)
                    )
                    or min(quote[k] for k in ("index", "bid", "ask", "bidSize", "askSize")) <= 0
                    or quote["ask"] < quote["bid"]
                    or any(
                        not normalized["minPrice"] <= quote[k] <= normalized["maxPrice"]
                        or quote[k] % normalized["tick"] != 0
                        for k in ("bid", "ask")
                    )
                ):
                    raise ValueError("Invalid or unavailable quote")
                normalized.update(quote)
            except (KeyError, ValueError, TypeError, ArithmeticError):
                pass
        options[symbol] = normalized
    return options


def load_snapshot(
    manifest_path: Path, max_age_seconds=30, *, minimum_hours=1, minimum_days=1, option_type="Call"
) -> dict:
    """Validate exact raw responses, freshness, pagination and completed candles."""
    if option_type not in {"Call", "Put"}:
        raise ValueError("Unsupported option type")
    manifest, records = _read_snapshot(manifest_path)
    times = _timing(manifest, records, max_age_seconds)
    clocks = [r for r in records if r["path"].endswith("/time")]
    if len(clocks) != 1 or records[0] is not clocks[0]:
        raise ValueError("First response must be the single exchange clock")
    cutoff = _integer(manifest["asOfMs"])
    if cutoff != times[0]:
        raise ValueError("asOfMs differs from first exchange clock")
    market = {"ms": max(times), "candles": {}, "options": {}}
    for base in BASES:

        def matches(suffix, interval=None, base=base):
            return [
                r
                for r in records
                if r["path"].endswith(suffix)
                and (
                    r["params"].get("baseCoin") == base
                    or r["params"].get("symbol") == base + "USDT"
                )
                and (interval is None or r["params"].get("interval") == interval)
                and (suffix != "/tickers" or r["params"].get("category") == "option")
            ]

        pages = matches("/instruments-info")
        tickers, hours, days = matches("/tickers"), matches("/kline", "60"), matches("/kline", "D")
        if any(len(group) != 1 for group in (tickers, hours, days)):
            raise ValueError("Missing or duplicate ticker/candle response")
        market["candles"][base] = {
            "hours": completed_candles(hours[0], cutoff, HOUR, minimum_hours),
            "days": completed_candles(days[0], cutoff, DAY, minimum_days),
        }
        market["options"].update(normalize_options(_catalog(pages), tickers[0], base, option_type))
    return market


def collect(output_parent: Path) -> Path:
    """Capture public local curl responses; return a validated manifest path."""
    output_parent = Path(output_parent)
    output_parent.mkdir(parents=True, exist_ok=True)
    output = Path(tempfile.mkdtemp(prefix="market-", dir=output_parent))
    destination = output / "manifest.json"
    manifest = {
        "startedUtc": _utc(),
        "transport": "local curl -q --noproxy '*'; public GET allowlist only",
        "records": [],
    }

    def get(path, params):
        _allowed(path, params)
        if _stamp(_utc()) - _stamp(manifest["startedUtc"]) > 30_000:
            raise ValueError("Snapshot duration exceeds 30 seconds")
        folder = Path(tempfile.mkdtemp(prefix="request-", dir=output))
        raw_path = folder / "response.json"
        sent = _utc()
        url = "https://api.bybit.com" + path + "?" + urllib.parse.urlencode(params)
        response = subprocess.run(  # noqa: S603 -- fixed curl and validated public GET allowlist
            [
                "/usr/bin/curl",
                "-q",
                "--noproxy",
                "*",
                "--max-time",
                "25",
                "--fail",
                "--silent",
                "--show-error",
                url,
            ],
            capture_output=True,
            check=True,
            timeout=30,
        )
        received = _utc()
        raw_path.write_bytes(response.stdout)
        record = {
            "path": path,
            "params": params,
            "startedUtc": sent,
            "receivedUtc": received,
            "file": str(raw_path.relative_to(output)),
            "sha256": hashlib.sha256(response.stdout).hexdigest(),
        }
        manifest["records"].append(record)
        destination.write_text(json.dumps(manifest, indent=2) + "\n")
        payload = json.loads(response.stdout)
        _timing({**manifest, "completedUtc": received}, [{**record, "payload": payload}], 30)
        return payload

    clock = get("/v5/market/time", {})
    manifest["asOfMs"] = _integer(clock["time"])
    for base in BASES:
        cursor = ""
        cursors = set()
        symbols = set()
        while True:
            params = {"category": "option", "baseCoin": base, "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            payload = get("/v5/market/instruments-info", params)
            rows = _rows({"params": params, "payload": payload})
            page_symbols = _unique_symbols(rows)
            if symbols.intersection(page_symbols):
                raise ValueError("Duplicate metadata symbol across pages")
            symbols.update(page_symbols)
            cursor = payload["result"].get("nextPageCursor", "")
            if not isinstance(cursor, str) or (cursor and (cursor in cursors or not rows)):
                raise ValueError("Invalid or repeated metadata cursor")
            if not cursor:
                break
            cursors.add(cursor)
        get("/v5/market/tickers", {"category": "option", "baseCoin": base})
        for interval, limit in (("60", 1000), ("D", 100)):
            get(
                "/v5/market/kline",
                {
                    "category": "spot",
                    "symbol": base + "USDT",
                    "interval": interval,
                    "limit": limit,
                    "end": manifest["asOfMs"],
                },
            )
    manifest["completedUtc"] = _utc()
    destination.write_text(json.dumps(manifest, indent=2) + "\n")
    load_snapshot(destination)
    return destination
