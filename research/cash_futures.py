"""现货－U 本位永续价差的只读采集与确定性回测。

范围（首期）：

* 仅 BTCUSDT / ETHUSDT，仅 Bybit 公共 REST 的现货与 linear 永续；
* 实时采集通过现有 SSH→NAS 通道，远端只用 Python 标准库 https，不携带任何密钥；
* 历史采集包含现货/永续 K 线、合约规格、资金费率历史；
* 回测使用「上一根完整 K 线选择信号、下一根开盘价成交」的保守时序，
  双边均按 taker 计手续费，并联立开仓/平仓的盘口价差代理与资金费率。

这不是实盘执行模块：不读取账户、不装载凭据、不发送任何交易写请求。
结果中的收益率是扣费后的模拟值，不能当作实际成交能力或收益承诺。
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import pathlib
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from bybitbot.money import common_step, dec, quantize_qty
from bybitbot.terms.fees import spot_fee

HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS
BASES = ("BTC", "ETH")
SYMBOLS = tuple(base + "USDT" for base in BASES)
ALLOWED_PATHS = frozenset(
    {
        "/v5/market/time",
        "/v5/market/instruments-info",
        "/v5/market/tickers",
        "/v5/market/orderbook",
        "/v5/market/kline",
        "/v5/market/funding/history",
    }
)
KLINE_INTERVALS = frozenset({"1", "5", "15", "60", "240", "D"})
_ZERO = Decimal("0")
_ONE = Decimal("1")


def utc_ms(value: str) -> int:
    """解析 YYYY-MM-DD（UTC 零点）为毫秒时间戳。"""
    moment = dt.datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=dt.UTC)
    return int(moment.timestamp() * 1000)


def ms_iso(value: int) -> str:
    return dt.datetime.fromtimestamp(value / 1000, tz=dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _decimal(value: Any) -> Decimal:
    if value is None or isinstance(value, bool):
        raise ValueError("Missing numeric value")
    try:
        result = dec(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("Invalid numeric value") from exc
    if not result.is_finite():
        raise ValueError("Non-finite numeric value")
    return result


def _integer(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("Invalid integer")
    result = int(value)
    if str(value) != str(result) and not isinstance(value, int):
        raise ValueError("Invalid integer")
    return result


def _file_digest(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _atomic_json(path: pathlib.Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with pathlib.os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        pathlib.os.replace(temporary, path)
    except BaseException:
        pathlib.Path(temporary).unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# 公开请求：只允许只读端点与固定参数集合
# ---------------------------------------------------------------------------


def _allowed_request(path: str, params: Mapping[str, Any]) -> dict[str, Any]:
    if path not in ALLOWED_PATHS or not isinstance(params, Mapping):
        raise ValueError("Public endpoint outside allowlist")
    result = dict(params.items())
    if path == "/v5/market/time":
        if result:
            raise ValueError("Unexpected time parameters")
    elif path == "/v5/market/instruments-info":
        if set(result) != {"category", "symbol"} or result["category"] not in ("spot", "linear"):
            raise ValueError("Unexpected instruments parameters")
        if result["symbol"] not in SYMBOLS:
            raise ValueError("Unexpected instruments symbol")
    elif path == "/v5/market/tickers":
        if set(result) != {"category", "symbol"} or result["category"] not in ("spot", "linear"):
            raise ValueError("Unexpected tickers parameters")
        if result["symbol"] not in SYMBOLS:
            raise ValueError("Unexpected tickers symbol")
    elif path == "/v5/market/orderbook":
        if set(result) != {"category", "symbol", "limit"} or result["category"] not in (
            "spot",
            "linear",
        ):
            raise ValueError("Unexpected orderbook parameters")
        if result["symbol"] not in SYMBOLS or result["limit"] not in (1, 25, 50):
            raise ValueError("Unexpected orderbook parameters")
    elif path == "/v5/market/kline":
        if set(result) != {"category", "symbol", "interval", "start", "end", "limit"}:
            raise ValueError("Unexpected kline parameters")
        if result["category"] not in ("spot", "linear") or result["symbol"] not in SYMBOLS:
            raise ValueError("Unexpected kline parameters")
        if result["interval"] not in KLINE_INTERVALS or result["limit"] not in (200, 1000):
            raise ValueError("Unexpected kline parameters")
        for name in ("start", "end"):
            value = result[name]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("Invalid kline window")
        if result["start"] >= result["end"]:
            raise ValueError("Invalid kline window")
    else:
        if not set(result) <= {"category", "symbol", "limit", "startTime", "endTime"}:
            raise ValueError("Unexpected funding parameters")
        if result.get("category") != "linear" or result.get("symbol") not in SYMBOLS:
            raise ValueError("Unexpected funding parameters")
        if result.get("limit") not in range(1, 201):
            raise ValueError("Unexpected funding parameters")
        for name in ("startTime", "endTime"):
            if name in result:
                value = result[name]
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise ValueError("Invalid funding window")
        if (
            "startTime" in result
            and "endTime" in result
            and result["startTime"] >= result["endTime"]
        ):
            raise ValueError("Invalid funding window")
    return result


# Python 标准库源码，经 SSH 输入到 NAS 主机执行；只读公开 GET 列表由本地白名单约束。
_REMOTE = r"""
import datetime as dt
import json
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

jobs = __JOBS__

def fetch(job):
    path, params = job
    last = ""
    for attempt in range(4):
        try:
            url = "https://api.bybit.com" + path + "?" + urllib.parse.urlencode(params)
            with urllib.request.urlopen(url, timeout=20) as response:
                payload = json.load(response)
            if payload.get("retCode") != 0:
                raise ValueError("retCode=" + str(payload.get("retCode")))
            return {"path": path, "params": params,
                    "receivedUtc": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "payload": payload}
        except Exception as error:
            last = type(error).__name__ + ": " + str(error)
            time.sleep(0.5 * (attempt + 1))
    return {"path": path, "params": params,
            "receivedUtc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "error": last}

started = dt.datetime.now(dt.timezone.utc).isoformat()
with ThreadPoolExecutor(max_workers=4) as pool:
    records = list(pool.map(fetch, jobs))
print(json.dumps({"startedUtc": started,
                  "completedUtc": dt.datetime.now(dt.timezone.utc).isoformat(),
                  "records": records}, separators=(",", ":")))
"""


def fetch_public_many(
    jobs: Sequence[tuple[str, dict[str, Any]]],
    *,
    ssh_host: str = "nas",
    timeout: int = 360,
) -> dict[str, Any]:
    """一次 SSH 会话取多个公开 GET 响应；远端只用标准库。"""
    prepared = [(path, _allowed_request(path, params)) for path, params in jobs]
    if not prepared:
        raise ValueError("No public requests")
    result = subprocess.run(  # noqa: S603 -- 固定 ssh 命令，请求参数先经白名单验证
        [
            "/usr/bin/ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            ssh_host,
            "python3 -I -",
        ],
        input=_REMOTE.replace("__JOBS__", json.dumps(prepared)),
        text=True,
        capture_output=True,
        check=True,
        timeout=timeout,
    )
    envelope = json.loads(result.stdout)
    if not isinstance(envelope.get("records"), list):
        raise ValueError("Malformed public response envelope")
    return envelope


def fetch_public(
    jobs: Sequence[tuple[str, dict[str, Any]]],
    *,
    ssh_host: str = "nas",
    timeout: int = 180,
) -> dict[str, Any]:
    return fetch_public_many(jobs, ssh_host=ssh_host, timeout=timeout)


def _records_for(
    envelope: Mapping[str, Any], path: str, params: Mapping[str, Any]
) -> list[dict[str, Any]]:
    wanted = dict(params.items())
    found = [
        record
        for record in envelope["records"]
        if record.get("path") == path and record.get("params") == wanted
    ]
    if len(found) != 1:
        raise ValueError(f"Missing or duplicate public response: {path} {wanted}")
    if "error" in found[0]:
        raise ValueError(f"Public collection failed: {found[0]['error']}")
    payload = found[0]["payload"]
    if payload.get("retCode") != 0:
        raise ValueError(f"Public API failure: {payload.get('retCode')}")
    return found


def _rows(envelope: Mapping[str, Any], path: str, params: Mapping[str, Any]) -> list:
    record = _records_for(envelope, path, params)[0]
    rows = record["payload"].get("result", {}).get("list")
    if not isinstance(rows, list):
        raise ValueError("Invalid response list")
    return rows


def _kline_jobs(
    category: str, symbol: str, start: int, end: int, width: int
) -> list[tuple[str, dict[str, Any]]]:
    """生成覆盖 [start, end) 的 K 线请求列表。

    接口按 ``end`` 向前返回最多 ``limit`` 根（含 ``end`` 那一根），因此这里按
    页宽切分窗口，页与页在边界行重叠 —— 重叠行在加载阶段按时间戳去重。
    """
    page_width = 1000 * width
    jobs: list[tuple[str, dict[str, Any]]] = []
    boundary = end
    while boundary > start:
        params = {
            "category": category,
            "symbol": symbol,
            "interval": "D" if width == DAY_MS else "60",
            "start": start,
            "end": boundary,
            "limit": 1000,
        }
        jobs.append(("/v5/market/kline", params))
        if boundary - page_width <= start:
            break
        boundary -= page_width
    return jobs


def _validate_klines(
    rows_by_symbol: Mapping[tuple[str, str], Sequence],
    start: int,
    end: int,
    width: int,
) -> dict[tuple[str, str], list[Candle]]:
    """校验 K 线完整性、时间连续性与跨市场同步（重叠页去重）。"""
    parsed: dict[tuple[str, str], list[Candle]] = {}
    for key, rows in rows_by_symbol.items():
        unique: dict[int, list] = {}
        for row in rows:
            if not isinstance(row, list) or len(row) != 7:
                raise ValueError("Malformed kline row")
            stamp = _integer(row[0])
            if stamp % width or not start <= stamp < end:
                raise ValueError("Kline outside requested window or misaligned")
            unique[stamp] = row
        validated = []
        for stamp in sorted(unique):
            row = unique[stamp]
            o, high, low, close = (_decimal(value) for value in row[1:5])
            volume, turnover = (_decimal(value) for value in row[5:7])
            if not (0 < low <= min(o, high, close) and max(o, high, close) <= high):
                raise ValueError("Invalid OHLC")
            if min(volume, turnover) < 0:
                raise ValueError("Invalid kline volume")
            validated.append(Candle(stamp, o, high, low, close))
        stamps = [candle.timestamp for candle in validated]
        if (
            len(stamps) != len(set(stamps))
            or any(b - a != width for a, b in zip(stamps, stamps[1:], strict=False))
        ):
            raise ValueError("Kline gap or duplicate")
        if not stamps or stamps[0] != start or stamps[-1] + width != end:
            raise ValueError("Incomplete kline window")
        parsed[key] = validated
    for symbol in SYMBOLS:
        spot_stamps = [c.timestamp for c in parsed[("spot", symbol)]]
        perp_stamps = [c.timestamp for c in parsed[("linear", symbol)]]
        if spot_stamps != perp_stamps:
            raise ValueError(f"Spot and perpetual candles differ: {symbol}")
    return parsed


# ---------------------------------------------------------------------------
# 快照与历史数据容器
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TopQuote:
    bid: Decimal
    ask: Decimal
    bid_size: Decimal
    ask_size: Decimal
    source_ms: int


@dataclass(frozen=True)
class ContractSpec:
    tick_size: Decimal
    qty_step: Decimal
    min_qty: Decimal

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> ContractSpec:
        try:
            price = payload["priceFilter"]
            lot = payload["lotSizeFilter"]
            return cls(
                tick_size=_decimal(price["tickSize"]),
                qty_step=_decimal(
                    lot["qtyStep"] if "qtyStep" in lot else lot["basePrecision"]
                ),
                min_qty=_decimal(lot["minOrderQty"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Incomplete contract specification") from exc


@dataclass(frozen=True)
class FundingPoint:
    timestamp: int
    rate: Decimal


@dataclass(frozen=True)
class Candle:
    timestamp: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


@dataclass
class HistoryData:
    start: int
    end: int
    width: int
    candles: dict[tuple[str, str], list[Candle]]
    funding: dict[str, list[FundingPoint]]
    specs: dict[str, ContractSpec]
    source_files: dict[str, str] = field(default_factory=dict)
    audit: dict[str, Any] = field(default_factory=dict)

    def candle(self, category: str, symbol: str) -> list[Candle]:
        return self.candles[(category, symbol)]


# ---------------------------------------------------------------------------
# 实时快照采集
# ---------------------------------------------------------------------------


def collect_live(output_parent: pathlib.Path, *, ssh_host: str = "nas") -> pathlib.Path:
    """采集一次双侧盘口/行情/规格，落原始响应与哈希清单。"""
    output_parent = pathlib.Path(output_parent)
    output_parent.mkdir(parents=True, exist_ok=True)
    output = pathlib.Path(tempfile.mkdtemp(prefix="cash-futures-", dir=output_parent))
    jobs: list[tuple[str, dict[str, Any]]] = [("/v5/market/time", {})]
    for symbol in SYMBOLS:
        for category in ("spot", "linear"):
            jobs.append(
                (
                    "/v5/market/orderbook",
                    {"category": category, "symbol": symbol, "limit": 25},
                )
            )
            jobs.append(("/v5/market/tickers", {"category": category, "symbol": symbol}))
            jobs.append(("/v5/market/instruments-info", {"category": category, "symbol": symbol}))
    envelope = fetch_public(jobs, ssh_host=ssh_host)
    records = []
    for index, (path, params) in enumerate(jobs):
        record = _records_for(envelope, path, params)[0]
        raw_path = output / f"response-{index:02d}.json"
        raw_path.write_text(
            json.dumps(record["payload"], separators=(",", ":"), sort_keys=True) + "\n",
            encoding="utf-8",
        )
        records.append(
            {
                "path": path,
                "params": params,
                "startedUtc": envelope["startedUtc"],
                "receivedUtc": record["receivedUtc"],
                "file": raw_path.name,
                "sha256": _file_digest(raw_path),
            }
        )
    manifest = {
        "kind": "cash-futures-live",
        "transport": "local ssh -> NAS stdlib https GET; public only; no credentials",
        "startedUtc": envelope["startedUtc"],
        "completedUtc": envelope["completedUtc"],
        "records": records,
    }
    destination = output / "manifest.json"
    _atomic_json(destination, manifest)
    normalize_live_snapshot(destination)
    return destination


def _top_from_book(payload: Mapping[str, Any], source_ms: int) -> TopQuote:
    result = payload["result"]
    bids = result.get("b", result.get("bids"))
    asks = result.get("a", result.get("asks"))
    if not bids or not asks:
        raise ValueError("Empty orderbook")
    bid, bid_size = (_decimal(value) for value in bids[0])
    ask, ask_size = (_decimal(value) for value in asks[0])
    if not (0 < bid < ask and min(bid_size, ask_size) > 0):
        raise ValueError("Invalid top of book")
    return TopQuote(bid, ask, bid_size, ask_size, source_ms)


def _top_from_ticker(payload: Mapping[str, Any], source_ms: int) -> TopQuote:
    row = payload["result"]["list"][0]
    bid, ask = _decimal(row["bid1Price"]), _decimal(row["ask1Price"])
    bid_size, ask_size = _decimal(row["bid1Size"]), _decimal(row["ask1Size"])
    if not (0 < bid < ask and min(bid_size, ask_size) > 0):
        raise ValueError("Invalid ticker quote")
    return TopQuote(bid, ask, bid_size, ask_size, source_ms)


def normalize_live_snapshot(manifest_path: pathlib.Path) -> dict[str, Any]:
    """校验原始响应哈希与报价时序，返回可直接计算价差的报价表。"""
    manifest_path = pathlib.Path(manifest_path)
    manifest = json.loads(manifest_path.read_bytes())
    root = manifest_path.parent.resolve(strict=True)
    payloads = {}
    for entry in manifest["records"]:
        raw_path = (root / entry["file"]).resolve(strict=True)
        if not raw_path.is_relative_to(root) or _file_digest(raw_path) != entry["sha256"]:
            raise ValueError("Raw response digest mismatch")
        payload = json.loads(raw_path.read_bytes())
        if payload.get("retCode") != 0:
            raise ValueError("Public API failure")
        _allowed_request(entry["path"], entry["params"])
        payloads[(entry["path"], json.dumps(entry["params"], sort_keys=True))] = payload
    clock = payloads[("/v5/market/time", "{}")]
    as_of = _integer(clock["time"])
    quotes: dict[str, dict[str, TopQuote]] = {}
    specs: dict[str, ContractSpec] = {}
    for symbol in SYMBOLS:
        quotes[symbol] = {}
        for category in ("spot", "linear"):
            book_payload = payloads[
                (
                    "/v5/market/orderbook",
                    json.dumps(
                        {"category": category, "symbol": symbol, "limit": 25}, sort_keys=True
                    ),
                )
            ]
            ticker_payload = payloads[
                (
                    "/v5/market/tickers",
                    json.dumps({"category": category, "symbol": symbol}, sort_keys=True),
                )
            ]
            source_ms = _integer(book_payload["result"]["ts"])
            top = _top_from_book(book_payload, source_ms)
            fallback = _top_from_ticker(ticker_payload, _integer(ticker_payload["time"]))
            if abs(top.bid - fallback.bid) > top.bid * Decimal("0.001") or abs(
                top.ask - fallback.ask
            ) > top.ask * Decimal("0.001"):
                raise ValueError("Orderbook and ticker quotes disagree")
            quotes[symbol][category] = top
            spec_payload = payloads[
                (
                    "/v5/market/instruments-info",
                    json.dumps({"category": category, "symbol": symbol}, sort_keys=True),
                )
            ]
            rows = spec_payload["result"]["list"]
            if len(rows) != 1:
                raise ValueError("Missing contract specification")
            specs[f"{category}:{symbol}"] = ContractSpec.from_payload(rows[0])
    return {"asOfMs": as_of, "quotes": quotes, "specs": specs, "manifest": str(manifest_path)}


def live_spread(view: Mapping[str, Any], symbol: str) -> dict[str, Any]:
    """计算同一时刻的可执行入场/退出价差；值均为 Decimal。"""
    quotes = view["quotes"][symbol]
    spot, perp = quotes["spot"], quotes["linear"]
    step = common_step(
        view["specs"][f"spot:{symbol}"].qty_step,
        view["specs"][f"linear:{symbol}"].qty_step,
    )
    entry = spot.ask - perp.bid
    exit_ = perp.ask - spot.bid
    return {
        "symbol": symbol,
        "sourceMs": max(spot.source_ms, perp.source_ms),
        "sourceSkewMs": abs(spot.source_ms - perp.source_ms),
        "spotBid": spot.bid,
        "spotAsk": spot.ask,
        "perpBid": perp.bid,
        "perpAsk": perp.ask,
        "entryBps": entry / spot.ask * Decimal(10_000),
        "exitBps": exit_ / spot.ask * Decimal(10_000),
        "roundtripBps": (spot.ask - perp.bid + perp.ask - spot.bid) / spot.ask * Decimal(10_000),
        "qtyStep": step,
        "minExecutableQty": max(spot.bid_size, spot.ask_size, perp.bid_size, perp.ask_size),
    }


# ---------------------------------------------------------------------------
# 历史采集
# ---------------------------------------------------------------------------


def collect_history(
    output_dir: pathlib.Path,
    *,
    start: str,
    end: str,
    interval: str = "1h",
    ssh_host: str = "nas",
) -> pathlib.Path:
    """下载现货/永续 K 线与资金费率；返回带 SHA-256 清单的数据目录。"""
    if interval not in ("1h", "4h"):
        raise ValueError("Only 1h and 4h history is supported")
    start_ms, end_ms = utc_ms(start), utc_ms(end)
    width = HOUR_MS if interval == "1h" else 4 * HOUR_MS
    if end_ms % width or start_ms % width or start_ms >= end_ms:
        raise ValueError("History boundaries must be aligned to the bar width")
    output = pathlib.Path(output_dir)
    if output.exists() and any(output.iterdir()):
        raise ValueError("History output directory must be empty")
    output.mkdir(parents=True, exist_ok=True)
    job_sets: list[tuple[str, dict[str, Any]]] = []
    funding: dict[str, list[FundingPoint]] = {}
    specs: dict[str, ContractSpec] = {}
    for symbol in SYMBOLS:
        for category in ("spot", "linear"):
            job_sets.extend(_kline_jobs(category, symbol, start_ms, end_ms, width))

            params = {"category": category, "symbol": symbol}
            envelope = fetch_public([("/v5/market/instruments-info", params)])
            ins_rows = _rows(envelope, "/v5/market/instruments-info", params)
            matched = [row for row in ins_rows if row.get("symbol") == symbol]
            if len(matched) != 1:
                raise ValueError("Missing contract specification")
            specs[f"{category}:{symbol}"] = ContractSpec.from_payload(matched[0])
            job_sets.append(("/v5/market/instruments-info", params))

        cursor = end_ms
        while cursor > start_ms:
            funding_params = {
                "category": "linear",
                "symbol": symbol,
                "limit": 200,
                "endTime": cursor,
            }
            envelope = fetch_public([("/v5/market/funding/history", funding_params)])
            rows = _rows(envelope, "/v5/market/funding/history", funding_params)
            job_sets.append(("/v5/market/funding/history", dict(funding_params)))
            for row in rows:
                stamp = _integer(row["fundingRateTimestamp"])
                if start_ms <= stamp < end_ms:
                    funding.setdefault(symbol, []).append(
                        FundingPoint(stamp, _decimal(row["fundingRate"]))
                    )
            if not rows:
                break
            oldest = min(_integer(row["fundingRateTimestamp"]) for row in rows)
            if oldest <= start_ms:
                break
            cursor = oldest
    if not job_sets:
        raise ValueError("No history requests")
    envelopes = fetch_public_many(job_sets, ssh_host=ssh_host)
    records = envelopes["records"]
    if len(records) != len(job_sets):
        raise ValueError("Missing public history response")
    raw_rows: dict[tuple[str, str], list] = {
        (category, symbol): [] for category in ("spot", "linear") for symbol in SYMBOLS
    }
    for record, (path, params) in zip(records, job_sets, strict=True):
        if path != record.get("path") or params != record.get("params"):
            raise ValueError("Public response order mismatch")
        if "error" in record:
            raise ValueError(f"Public collection failed: {record['error']}")
        payload = record["payload"]
        if payload.get("retCode") != 0:
            raise ValueError("Public API failure")
        if path == "/v5/market/kline":
            key = (params["category"], params["symbol"])
            # 接口会把 end 边界上的那根 K 线一并返回（该根未完成/不属于窗口），丢弃。
            raw_rows[key].extend(
                row for row in payload["result"]["list"] if _integer(row[0]) < end_ms
            )
    candles = _validate_klines(raw_rows, start_ms, end_ms, width)
    for symbol in SYMBOLS:
        if not funding.get(symbol):
            raise ValueError(f"Funding history missing: {symbol}")
    _write_history(output, start_ms, end_ms, width, candles, funding, specs, job_sets)
    return output / "manifest.json"


def _write_history(
    output: pathlib.Path,
    start: int,
    end: int,
    width: int,
    candles: dict[tuple[str, str], list[Candle]],
    funding: dict[str, list[FundingPoint]],
    specs: dict[str, ContractSpec],
    request_log: list[tuple[str, dict[str, Any]]],
) -> None:
    files: dict[str, str] = {}
    for (category, symbol), rows in candles.items():
        name = f"kline-{category}-{symbol}.csv"
        with (output / name).open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["timestamp", "open", "high", "low", "close"])
            for row in rows:
                writer.writerow(
                    [row.timestamp, row.open, row.high, row.low, row.close]
                )
        files[name] = _file_digest(output / name)
    for symbol, rows in funding.items():
        name = f"funding-{symbol}.csv"
        with (output / name).open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["timestamp", "rate"])
            for row in sorted(rows, key=lambda item: item.timestamp):
                writer.writerow([row.timestamp, row.rate])
        files[name] = _file_digest(output / name)
    spec_payload = {
        name: {
            "tick_size": str(spec.tick_size),
            "qty_step": str(spec.qty_step),
            "min_qty": str(spec.min_qty),
        }
        for name, spec in specs.items()
    }
    (output / "specs.json").write_text(
        json.dumps(spec_payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    files["specs.json"] = _file_digest(output / "specs.json")
    seen: set[tuple[str, str]] = set()
    unique_requests = []
    for path, params in request_log:
        key = (path, json.dumps(params, sort_keys=True))
        if key in seen:
            continue
        seen.add(key)
        unique_requests.append({"path": path, "params": params})
    manifest = {
        "kind": "cash-futures-history",
        "startMs": start,
        "endMs": end,
        "intervalMs": width,
        "transport": "local ssh -> NAS stdlib https GET; public only; no credentials",
        "requests": unique_requests,
        "files": files,
    }
    _atomic_json(output / "manifest.json", manifest)


def load_history(path: pathlib.Path) -> HistoryData:
    """严格校验历史清单：哈希、时间连续性、数值与跨市场时间对齐。"""
    path = pathlib.Path(path)
    manifest = json.loads(path.read_bytes())
    if manifest.get("kind") != "cash-futures-history":
        raise ValueError("Not a cash-futures history manifest")
    root = path.parent.resolve(strict=True)
    for name, digest in manifest["files"].items():
        target = (root / name).resolve(strict=True)
        if not target.is_relative_to(root) or _file_digest(target) != digest:
            raise ValueError("History file digest mismatch")
    start, end, width = (
        _integer(manifest["startMs"]),
        _integer(manifest["endMs"]),
        _integer(manifest["intervalMs"]),
    )
    candles = {}
    for category in ("spot", "linear"):
        for symbol in SYMBOLS:
            name = f"kline-{category}-{symbol}.csv"
            rows = []
            with (root / name).open(encoding="utf-8", newline="") as handle:
                for item in csv.DictReader(handle):
                    rows.append(
                        Candle(
                            timestamp=_integer(item["timestamp"]),
                            open=_decimal(item["open"]),
                            high=_decimal(item["high"]),
                            low=_decimal(item["low"]),
                            close=_decimal(item["close"]),
                        )
                    )
            stamps = [row.timestamp for row in rows]
            if (
                len(stamps) != len(set(stamps))
                or any(b - a != width for a, b in zip(stamps, stamps[1:], strict=False))
                or stamps[0] != start
                or stamps[-1] + width != end
            ):
                raise ValueError(f"Incomplete or misaligned candles: {name}")
            candles[(category, symbol)] = rows
    for (spot_category, symbol), rows in candles.items():
        if spot_category != "spot":
            continue
        perp = candles[("linear", symbol)]
        if [row.timestamp for row in rows] != [row.timestamp for row in perp]:
            raise ValueError(f"Spot and perpetual candles differ: {symbol}")
    funding: dict[str, list[FundingPoint]] = {}
    for symbol in SYMBOLS:
        name = f"funding-{symbol}.csv"
        rows = []
        with (root / name).open(encoding="utf-8", newline="") as handle:
            for item in csv.DictReader(handle):
                stamp = _integer(item["timestamp"])
                if start <= stamp < end:
                    rows.append(FundingPoint(stamp, _decimal(item["rate"])))
        funding[symbol] = sorted(rows, key=lambda item: item.timestamp)
    specs = {
        name: ContractSpec(
            tick_size=_decimal(payload["tick_size"]),
            qty_step=_decimal(payload["qty_step"]),
            min_qty=_decimal(payload["min_qty"]),
        )
        for name, payload in json.loads((root / "specs.json").read_bytes()).items()
    }
    return HistoryData(start, end, width, candles, funding, specs, manifest["files"], manifest)


# ---------------------------------------------------------------------------
# 回测：价差收益、成本、资金费与资金曲线
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CostModel:
    """版本化的成本口径；分数均为比例，滑点为单边 bps。

    费率按 Bybit 非 VIP 公开费率取保守值：现货 taker 0.10%，永续 taker
    0.055%、maker 0.02%。maker 口径用于「挂单成交」的入场腿，taker 用于
    需要立即成交的平仓腿。
    """

    version: str
    spot_taker_rate: Decimal = Decimal("0.001")
    perp_taker_rate: Decimal = Decimal("0.00055")
    perp_maker_rate: Decimal = Decimal("0.0002")
    spot_maker_rate: Decimal = Decimal("0.001")
    #: 用 K 线收盘价差代理下一根开盘时的双边盘口成本，按 bps 计。
    spread_slippage_bps: Decimal = Decimal("10")
    #: 单腿成交失败/滑点放大的压力倍数。
    stress_multiplier: Decimal = Decimal("2")

    def __post_init__(self) -> None:
        if not self.version:
            raise ValueError("Cost model requires a version")
        for name in (
            "spot_taker_rate",
            "perp_taker_rate",
            "perp_maker_rate",
            "spot_maker_rate",
            "spread_slippage_bps",
            "stress_multiplier",
        ):
            value = getattr(self, name)
            if not value.is_finite() or value < 0:
                raise ValueError(f"{name} must be non-negative and finite")


@dataclass(frozen=True)
class StrategyConfig:
    """入场/退出阈值。

    ``mode``：

    * ``basis`` —— 基差套利。需要永续相对现货升水（``short_perp``）或现货
      相对永续升水（``short_spot``），赚取基差收敛。
    * ``funding_capture`` —— 资金费捕获。永续贴水时并不要求升水，而是要求
      **已结算的资金费足够高**：做空永续收资金费，用现货多头对冲价格风险。
      贴水本身是持仓成本（基差会向上收敛），必须计入净收益而不是当成利润。

    ``direction``：

    * ``short_perp`` —— 买现货、卖永续；
    * ``short_spot`` —— 卖现货、买永续。卖现货需要借贷，本仓库现货默认不
      借贷，因此该方向仅作研究对照。
    """

    version: str
    mode: str = "basis"
    direction: str = "short_perp"
    entry_bps: Decimal = Decimal("15")
    exit_bps: Decimal = Decimal("2")
    funding_hold_bars: int = 24
    max_hold_bars: int = 168
    capital_per_symbol: Decimal = Decimal("1000")
    stop_loss_bps: Decimal = Decimal("60")
    #: 入场腿用 maker 挂单（省手续费），平仓腿用 taker。
    maker_entry: bool = True
    #: 资金费捕获模式的年化门槛（比例）；入场要求最近一次已结算资金费不低于它。
    funding_entry_annual_pct: Decimal = Decimal("0.05")
    #: 资金费捕获模式的平仓阈值（比例）；最近一次已结算资金费低于它即退出。
    #: 取负值表示「容忍负 carry 也继续持有」，只在资金费跌到该阈值以下才走。
    funding_exit_annual_pct: Decimal = Decimal("0")
    #: 资金费捕获模式允许的最大不利基差（bps），超过则放弃该次机会。
    max_adverse_basis_bps: Decimal = Decimal("20")

    def __post_init__(self) -> None:
        if not self.version:
            raise ValueError("Strategy requires a version")
        if self.mode not in ("basis", "funding_capture"):
            raise ValueError("Unknown mode")
        if self.direction not in ("short_perp", "short_spot"):
            raise ValueError("Unknown direction")
        if self.mode == "basis" and (self.entry_bps <= self.exit_bps or self.exit_bps < 0):
            raise ValueError("Entry threshold must exceed exit threshold")
        if not 0 < self.funding_hold_bars <= self.max_hold_bars:
            raise ValueError("Invalid holding periods")
        if not self.capital_per_symbol.is_finite() or self.capital_per_symbol <= 0:
            raise ValueError("Capital must be positive")
        if not self.stop_loss_bps.is_finite() or self.stop_loss_bps < 0:
            raise ValueError("Invalid stop loss")
        for name in ("funding_entry_annual_pct", "funding_exit_annual_pct"):
            value = getattr(self, name)
            if not value.is_finite():
                raise ValueError(f"{name} must be finite")
        # 入场门槛要求正 carry；平仓阈值允许为负，表示愿意扛过负 carry。
        if self.funding_entry_annual_pct < 0:
            raise ValueError("funding_entry_annual_pct must be non-negative")
        if self.funding_exit_annual_pct < -1:
            raise ValueError("funding_exit_annual_pct must be at least -1")
        if not self.max_adverse_basis_bps.is_finite() or self.max_adverse_basis_bps < 0:
            raise ValueError("Invalid adverse basis bound")


@dataclass
class Trade:
    symbol: str
    entry_index: int
    entry_time: int
    exit_index: int
    exit_time: int
    qty: Decimal
    spot_entry: Decimal
    perp_entry: Decimal
    spot_exit: Decimal
    perp_exit: Decimal
    spot_fees: Decimal
    perp_fees: Decimal
    funding: Decimal
    slippage: Decimal
    pnl: Decimal
    reason: str
    funding_periods: int = 0

    @property
    def net_bps(self) -> Decimal:
        if self.spot_entry == 0:
            return _ZERO
        return self.pnl / (self.spot_entry * self.qty) * Decimal(10_000)


@dataclass
class BacktestResult:
    strategy_version: str
    cost_version: str
    interval_ms: int
    start: int
    end: int
    trades: list[Trade]
    equity: list[tuple[int, Decimal]]
    rejected: dict[str, int]

    def metrics(self) -> dict[str, Any]:
        pnl = sum((trade.pnl for trade in self.trades), _ZERO)
        wins = sum(trade.pnl > 0 for trade in self.trades)
        fees = sum((trade.spot_fees + trade.perp_fees for trade in self.trades), _ZERO)
        funding = sum((trade.funding for trade in self.trades), _ZERO)
        slippage = sum((trade.slippage for trade in self.trades), _ZERO)
        gross = pnl + fees + slippage - funding
        return {
            "strategy_version": self.strategy_version,
            "cost_version": self.cost_version,
            "interval": ms_iso(self.start) + "/" + ms_iso(self.end),
            "trades": len(self.trades),
            "wins": wins,
            "losses": len(self.trades) - wins,
            "gross_pnl": gross,
            "fees": fees,
            "funding_received": funding,
            "slippage_cost": slippage,
            "net_pnl": pnl,
            "rejected": dict(self.rejected),
        }


def _gap_bps(spot: Candle, perp: Candle) -> Decimal:
    if min(spot.close, perp.close) <= 0:
        raise ValueError("Nonpositive close")
    return (perp.close - spot.close) / spot.close * Decimal(10_000)


def _funding_between(points: Sequence[FundingPoint], start: int, end: int) -> tuple[Decimal, int]:
    """半开区间 [start, end) 内已结算的资金费。"""
    selected = [point for point in points if start <= point.timestamp < end]
    return sum((point.rate for point in selected), _ZERO), len(selected)


def _latest_funding(points: Sequence[FundingPoint], at_ms: int) -> FundingPoint | None:
    """决策时刻**已经结算**的最近一次资金费；未结算的不能当信号。"""
    known = [point for point in points if point.timestamp <= at_ms]
    return max(known, key=lambda point: point.timestamp) if known else None


def _annualized_funding(rate: Decimal, width_ms: int) -> Decimal:
    """把单次结算费率折算成年化比例（按实际结算间隔推断每日子数）。"""
    if rate == 0:
        return _ZERO
    per_day = Decimal(DAY_MS) / Decimal(width_ms)
    return rate * per_day * Decimal(365)


def _fee(price: Decimal, qty: Decimal, rate: Decimal) -> Decimal:
    return spot_fee(price=price, qty=qty, rate=rate)


def run_backtest(
    data: HistoryData,
    strategy: StrategyConfig,
    costs: CostModel,
) -> BacktestResult:
    """以完整收盘 K 线决策、下一根开盘成交，逐笔模拟双腿对冲。

    ``short_perp``：买现货、卖永续 —— 永续升水时入场，赚基差收敛与正资金费。
    ``short_spot``：卖现货、买永续 —— 永续贴水时入场，赚贴水收敛；卖现货
    需要借贷，本仓库现货默认不借贷，因此该方向只作研究对照。
    """
    if not isinstance(data, HistoryData):
        raise TypeError("Expected validated HistoryData")
    bars = (data.end - data.start) // data.width
    if bars < strategy.max_hold_bars + 2:
        raise ValueError("History too short for configured holding period")
    rejected = {"no_next_bar": 0, "below_entry": 0, "invalid_qty": 0, "open_too_late": 0}
    if strategy.mode == "funding_capture":
        rejected["funding_too_low"] = 0
        rejected["basis_too_adverse"] = 0
        rejected["no_settled_funding"] = 0
    trades: list[Trade] = []
    sign = Decimal(1) if strategy.direction == "short_perp" else Decimal(-1)

    def _close(
        position: Trade,
        *,
        spot_price: Decimal,
        perp_price: Decimal,
        exit_time: int,
        exit_index: int,
        reason: str,
    ) -> Trade:
        """平掉一条腿对冲仓位并结清损益（含平仓费、滑点与区间资金费）。"""
        position.spot_exit = spot_price
        position.perp_exit = perp_price
        position.exit_index = exit_index
        position.exit_time = exit_time
        position.spot_fees += _fee(spot_price, position.qty, costs.spot_taker_rate)
        position.perp_fees += _fee(perp_price, position.qty, costs.perp_taker_rate)
        position.slippage += (
            spot_price
            * position.qty
            * Decimal(2)
            * costs.spread_slippage_bps
            / Decimal(10_000)
            * costs.stress_multiplier
        )
        rate, count = _funding_between(
            funding_points,
            position.entry_time,
            exit_time,
        )
        # 空永续收正资金费（rate>0 时多头付空头）；买永续时反向。
        position.funding = sign * rate * position.qty * position.perp_entry
        position.funding_periods = count
        position.pnl = (
            sign * (position.perp_entry - position.perp_exit) * position.qty
            + sign * (position.spot_exit - position.spot_entry) * position.qty
            + position.funding
            - position.spot_fees
            - position.perp_fees
            - position.slippage
        )
        position.reason = reason
        return position

    for symbol in SYMBOLS:
        spot, perp = data.candle("spot", symbol), data.candle("linear", symbol)
        funding_points = data.funding[symbol]
        funding_interval = data.width
        if len(funding_points) > 1:
            intervals = [
                b.timestamp - a.timestamp
                for a, b in zip(funding_points, funding_points[1:], strict=False)
                if b.timestamp > a.timestamp
            ]
            if intervals:
                funding_interval = min(intervals)
        step = common_step(
            data.specs[f"spot:{symbol}"].qty_step,
            data.specs[f"linear:{symbol}"].qty_step,
        )
        min_qty = max(
            data.specs[f"spot:{symbol}"].min_qty,
            data.specs[f"linear:{symbol}"].min_qty,
        )
        entry_spot_rate = (
            costs.spot_maker_rate if strategy.maker_entry else costs.spot_taker_rate
        )
        entry_perp_rate = (
            costs.perp_maker_rate if strategy.maker_entry else costs.perp_taker_rate
        )
        position: Trade | None = None
        for index in range(bars - 1):
            gap = _gap_bps(spot[index], perp[index]) * sign
            if position is None:
                if strategy.mode == "funding_capture":
                    known = _latest_funding(funding_points, spot[index].timestamp)
                    if known is None:
                        rejected["no_settled_funding"] += 1
                        continue
                    annualized = _annualized_funding(known.rate, funding_interval)
                    if annualized < strategy.funding_entry_annual_pct:
                        rejected["funding_too_low"] += 1
                        continue
                    if gap < -strategy.max_adverse_basis_bps:
                        rejected["basis_too_adverse"] += 1
                        continue
                elif gap < strategy.entry_bps:
                    rejected["below_entry"] += 1
                    continue
                if index + 1 >= bars:
                    rejected["no_next_bar"] += 1
                    continue
                entry_spot, entry_perp = spot[index + 1].open, perp[index + 1].open
                if min(entry_spot, entry_perp) <= 0:
                    rejected["no_next_bar"] += 1
                    continue
                qty = quantize_qty(strategy.capital_per_symbol / entry_spot, step)
                if qty < min_qty:
                    rejected["invalid_qty"] += 1
                    continue
                # 滑点按名义额计：四条腿（现货开/平、永续开/平）各承担一次
                # 单边滑点，乘以压力倍数。用 K 线收盘价差只是价差水平的代理，
                # 因此这里不按基差大小缩放。
                slippage = (
                    entry_spot
                    * qty
                    * Decimal(2)
                    * costs.spread_slippage_bps
                    / Decimal(10_000)
                    * costs.stress_multiplier
                )
                position = Trade(
                    symbol=symbol,
                    entry_index=index + 1,
                    entry_time=spot[index + 1].timestamp,
                    exit_index=index + 1,
                    exit_time=spot[index + 1].timestamp,
                    qty=qty,
                    spot_entry=entry_spot,
                    perp_entry=entry_perp,
                    spot_exit=_ZERO,
                    perp_exit=_ZERO,
                    spot_fees=_fee(entry_spot, qty, entry_spot_rate),
                    perp_fees=_fee(entry_perp, qty, entry_perp_rate),
                    funding=_ZERO,
                    slippage=slippage,
                    pnl=_ZERO,
                    reason="",
                )
                continue
            held = index - position.entry_index
            stop = gap < -strategy.stop_loss_bps
            time_exit = held >= strategy.max_hold_bars
            if strategy.mode == "funding_capture":
                known = _latest_funding(funding_points, spot[index].timestamp)
                funding_exit = (
                    known is not None
                    and _annualized_funding(known.rate, funding_interval)
                    <= strategy.funding_exit_annual_pct
                    and held >= strategy.funding_hold_bars
                )
            else:
                funding_exit = held >= strategy.funding_hold_bars and gap <= strategy.exit_bps
            if not (stop or time_exit or funding_exit):
                continue
            if index + 1 >= bars:
                rejected["no_next_bar"] += 1
                continue
            exit_spot, exit_perp = spot[index + 1].open, perp[index + 1].open
            if min(exit_spot, exit_perp) <= 0:
                rejected["no_next_bar"] += 1
                continue
            reason = "stop" if stop else "time" if time_exit else "funding"
            trades.append(
                _close(
                    position,
                    spot_price=exit_spot,
                    perp_price=exit_perp,
                    exit_time=spot[index + 1].timestamp,
                    exit_index=index + 1,
                    reason=reason,
                )
            )
            position = None
        # 样本末尾仍持仓：按最后一根收盘价盯市平掉，把未结算的资金费、
        # 平仓手续费和滑点全部计入，避免长持有策略被系统性漏算。
        if position is not None:
            trades.append(
                _close(
                    position,
                    spot_price=spot[bars - 1].close,
                    perp_price=perp[bars - 1].close,
                    exit_time=data.end,
                    exit_index=bars,
                    reason="eod",
                )
            )
            position = None
    trades.sort(key=lambda trade: trade.exit_time)
    running = _ZERO
    by_time: dict[int, Decimal] = {}
    for trade in trades:
        running += trade.pnl
        by_time[trade.exit_time] = running
    equity: list[tuple[int, Decimal]] = []
    current = _ZERO
    for index in range(bars + 1):
        stamp = data.start + index * data.width
        current = by_time.get(stamp, current)
        equity.append((stamp, current))
    return BacktestResult(
        strategy_version=strategy.version,
        cost_version=costs.version,
        interval_ms=data.width,
        start=data.start,
        end=data.end,
        trades=trades,
        equity=equity,
        rejected=rejected,
    )


def scan_opportunity(
    data: HistoryData,
    costs: CostModel,
    *,
    direction: str = "short_perp",
) -> dict[str, Any]:
    """统计「按当前费率是否存在可交易价差」，不产生任何交易结论。

    ``direction`` 为 ``short_perp``（买现货、卖永续，需要永续升水）或
    ``short_spot``（卖现货、买永续，需要现货升水；卖现货需要借贷，本仓库
    的现货默认不借贷，因此仅作为对照口径）。
    """
    if direction not in ("short_perp", "short_spot"):
        raise ValueError("direction must be short_perp or short_spot")
    roundtrip_fee_bps = (
        costs.spot_taker_rate * 2 + costs.perp_taker_rate * 2
    ) * Decimal(10_000)
    hurdle_bps = roundtrip_fee_bps + costs.spread_slippage_bps * costs.stress_multiplier
    per_symbol = []
    for symbol in SYMBOLS:
        spot, perp = data.candle("spot", symbol), data.candle("linear", symbol)
        gaps = [
            (perp.close - spot.close) / spot.close * Decimal(10_000)
            for spot, perp in zip(spot, perp, strict=True)
        ]
        ordered = sorted(gaps)
        count = len(ordered)
        funding = data.funding[symbol]
        funding_rates = [point.rate for point in funding]
        positive = sum(1 for rate in funding_rates if rate > 0)
        # 按 UTC 自然日汇总资金费，得到「每天实际结算的费率之和」。
        daily: dict[int, Decimal] = {}
        for point in funding:
            day = point.timestamp - point.timestamp % DAY_MS
            daily[day] = daily.get(day, _ZERO) + point.rate
        daily_values = list(daily.values())
        mean_daily = (
            sum(daily_values, _ZERO) / len(daily_values) if daily_values else _ZERO
        )
        if direction == "short_perp":
            edge = ordered[-1]
            edge_bps = edge
            qualifying = sum(1 for gap in gaps if gap >= hurdle_bps)
        else:
            edge = ordered[0]
            edge_bps = -edge
            qualifying = sum(1 for gap in gaps if -gap >= hurdle_bps)
        per_symbol.append(
            {
                "symbol": symbol,
                "bars": count,
                "basis_min_bps": ordered[0],
                "basis_p50_bps": ordered[count // 2],
                "basis_p90_bps": ordered[int(count * 0.9)],
                "basis_max_bps": ordered[-1],
                "edge_bps": edge_bps,
                "bars_over_hurdle": qualifying,
                "funding_settlements": len(funding_rates),
                "funding_positive_fraction": (
                    positive / len(funding_rates) if funding_rates else None
                ),
                "funding_mean_daily_bps": mean_daily * Decimal(10_000),
                "funding_annualized_pct": mean_daily * Decimal(365) * Decimal(100),
                "days_to_cover_fees": (
                    (roundtrip_fee_bps / (mean_daily * Decimal(10_000)))
                    if mean_daily > 0
                    else None
                ),
            }
        )
    return {
        "direction": direction,
        "roundtrip_fee_bps": roundtrip_fee_bps,
        "slippage_bps": costs.spread_slippage_bps * costs.stress_multiplier,
        "hurdle_bps": hurdle_bps,
        "symbols": per_symbol,
        "note": (
            "扫描只统计公开历史价差与资金费；不构成交易信号，也不代表未来可成交。"
        ),
    }


def save_report(result: BacktestResult, path: pathlib.Path) -> None:
    payload = {
        "metrics": {
            key: str(value) if isinstance(value, Decimal) else value
            for key, value in result.metrics().items()
        },
        "trades": [
            {
                **{
                    key: str(value) if isinstance(value, Decimal) else value
                    for key, value in trade.__dict__.items()
                },
                "net_bps": str(trade.net_bps),
            }
            for trade in result.trades
        ],
        "equity": [[stamp, str(value)] for stamp, value in result.equity],
    }
    _atomic_json(path, payload)


def main() -> None:
    parser = argparse.ArgumentParser(description="Bybit 现货/永续价差只读采集与回测")
    sub = parser.add_subparsers(dest="command", required=True)
    live = sub.add_parser("collect-live", help="采集一次实时盘口快照")
    live.add_argument("--out", required=True)
    live.add_argument("--ssh-host", default="nas")
    history = sub.add_parser("collect-history", help="采集历史 K 线与资金费率")
    history.add_argument("--out", required=True)
    history.add_argument("--start", required=True, help="UTC YYYY-MM-DD")
    history.add_argument("--end", required=True, help="UTC YYYY-MM-DD")
    history.add_argument("--interval", choices=("1h", "4h"), default="1h")
    history.add_argument("--ssh-host", default="nas")
    backtest = sub.add_parser("backtest", help="从已校验历史数据回测")
    backtest.add_argument("--history", required=True, help="manifest.json 路径")
    backtest.add_argument("--out", required=True, help="JSON 报告路径")
    backtest.add_argument("--entry-bps", default="15")
    backtest.add_argument("--exit-bps", default="2")
    backtest.add_argument("--max-hold-bars", type=int, default=168)
    backtest.add_argument("--funding-hold-bars", type=int, default=24)
    backtest.add_argument("--capital", default="1000")
    backtest.add_argument("--mode", choices=("basis", "funding_capture"), default="basis")
    backtest.add_argument("--direction", choices=("short_perp", "short_spot"), default="short_perp")
    backtest.add_argument("--funding-entry-annual-pct", default="0.05")
    backtest.add_argument("--funding-exit-annual-pct", default="0")
    backtest.add_argument("--max-adverse-basis-bps", default="20")
    backtest.add_argument("--taker-entry", action="store_true", help="入场也按 taker 计费")
    scan = sub.add_parser("scan", help="统计历史价差与资金费，判断是否存在可交易空间")
    scan.add_argument("--history", required=True, help="manifest.json 路径")
    scan.add_argument("--direction", choices=("short_perp", "short_spot"), default="short_perp")
    live_spread_parser = sub.add_parser("live-spread", help="读取已采集快照，输出可执行价差")
    live_spread_parser.add_argument("manifest")
    args = parser.parse_args()
    if args.command == "collect-live":
        print(collect_live(pathlib.Path(args.out), ssh_host=args.ssh_host))
    elif args.command == "live-spread":
        view = normalize_live_snapshot(pathlib.Path(args.manifest))
        print(json.dumps([live_spread(view, symbol) for symbol in SYMBOLS], indent=2, default=str))
    elif args.command == "collect-history":
        print(
            collect_history(
                pathlib.Path(args.out),
                start=args.start,
                end=args.end,
                interval=args.interval,
                ssh_host=args.ssh_host,
            )
        )
    elif args.command == "scan":
        data = load_history(pathlib.Path(args.history))
        report = scan_opportunity(
            data, CostModel(version="bybit-cash-futures-v1"), direction=args.direction
        )
        print(json.dumps(report, indent=2, default=str))
    else:
        data = load_history(pathlib.Path(args.history))
        result = run_backtest(
            data,
            StrategyConfig(
                version="cash-futures-v1",
                mode=args.mode,
                direction=args.direction,
                entry_bps=_decimal(args.entry_bps),
                exit_bps=_decimal(args.exit_bps),
                max_hold_bars=args.max_hold_bars,
                funding_hold_bars=args.funding_hold_bars,
                capital_per_symbol=_decimal(args.capital),
                funding_entry_annual_pct=_decimal(args.funding_entry_annual_pct),
                funding_exit_annual_pct=_decimal(args.funding_exit_annual_pct),
                max_adverse_basis_bps=_decimal(args.max_adverse_basis_bps),
                maker_entry=not args.taker_entry,
            ),
            CostModel(version="bybit-cash-futures-v1"),
        )
        save_report(result, pathlib.Path(args.out))
        print(json.dumps(result.metrics(), indent=2, default=str))


if __name__ == "__main__":
    main()
