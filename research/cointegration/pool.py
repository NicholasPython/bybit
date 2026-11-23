"""标的池构建与协整评估（PRD FR-07.1，离线进化平面）。

数据来源只有 Bybit V5 **公共** REST：``tickers``（流动性排序）、``instruments-info``
（区分永续与交割）、``kline``（小时级收盘价）。本机直连交易所的出口不通，
因此取数走仓库既有的 SSH→NAS 通道：远端只用 Python 标准库发只读 GET，
不携带任何凭据、不做任何写请求。

本模块不写数据库、不改配置、不触碰交易 worker 的运行路径；它只产出供人工
审阅的报告。
"""

from __future__ import annotations

import json
import math
import os
import pathlib
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from cointegration import PairEvaluation, evaluate_pair

HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS

#: 允许的公共端点：只读，且都不需要凭据。
ALLOWED_PATHS = frozenset(
    {
        "/v5/market/tickers",
        "/v5/market/instruments-info",
        "/v5/market/kline",
    }
)

#: 流动性排序与协整扫描的默认规模（PRD FR-07.1：前 50 合约、60 天）。
DEFAULT_TOP_SYMBOLS = 50
DEFAULT_DAYS = 60
#: 判定"样本够用"的门槛：小时级 500 条约合 20 天。
DEFAULT_MIN_OBSERVATIONS = 500

Fetcher = Callable[[Sequence[tuple[str, dict[str, Any]]]], Mapping[str, Any]]


class PublicDataError(RuntimeError):
    """公开数据采集失败。"""


@dataclass(frozen=True)
class SymbolQuote:
    """一个线性合约的公开行情摘要。"""

    symbol: str
    turnover_24h: float
    contract_type: str
    status: str
    #: 标的币种；同一个 base coin 的两个合约（例如 BTCUSDT 与 BTCPERP）
    #: 不是可做的价差对，会被排除。
    base_coin: str


@dataclass(frozen=True)
class PairResult:
    """一个配对的评估结果与归档分类。

    分类：``keep``（在册且仍协整）、``drop``（在册但已破裂）、
    ``nominate``（不在册但通过检验，提名待人工审阅）。
    """

    evaluation: PairEvaluation
    classification: str

    def as_dict(self) -> dict[str, Any]:
        evaluation = self.evaluation
        cointegration = evaluation.cointegration
        return {
            "pair": f"{evaluation.symbol_a}:{evaluation.symbol_b}",
            "symbol_a": evaluation.symbol_a,
            "symbol_b": evaluation.symbol_b,
            "classification": self.classification,
            "cointegrated": evaluation.cointegrated,
            "reason": evaluation.reason,
            "observations": evaluation.observations,
            "span_hours": evaluation.span_hours,
            "hedge_ratio": cointegration.hedge_ratio,
            "intercept": cointegration.intercept,
            "adf_statistic": cointegration.adf_statistic,
            "p_value": cointegration.p_value,
            "adf_lags": cointegration.adf_lags,
            "half_life_hours": cointegration.half_life,
            "correlation": cointegration.correlation,
            "adf_a_statistic": evaluation.adf_a.statistic,
            "adf_a_p_value": evaluation.adf_a.p_value,
            "adf_b_statistic": evaluation.adf_b.statistic,
            "adf_b_p_value": evaluation.adf_b.p_value,
            "both_i1": evaluation.both_integration_order_one,
            "half_rejections": evaluation.half_rejections,
            "halves": evaluation.halves,
            "stable": evaluation.stable,
        }


def _allowed_request(path: str, params: Mapping[str, Any]) -> dict[str, Any]:
    """只允许固定端点与受限参数。"""
    if path not in ALLOWED_PATHS or not isinstance(params, Mapping):
        raise ValueError("公共端点不在白名单内")
    result = dict(params.items())
    if path == "/v5/market/tickers":
        if set(result) != {"category"} or result["category"] != "linear":
            raise ValueError("Unexpected tickers parameters")
    elif path == "/v5/market/instruments-info":
        if set(result) - {"category", "limit", "cursor"}:
            raise ValueError("Unexpected instruments parameters")
        if result["category"] != "linear":
            raise ValueError("Unexpected instruments parameters")
        limit = result.get("limit", 1000)
        if not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("Unexpected instruments limit")
        cursor = result.get("cursor")
        if cursor is not None and (not isinstance(cursor, str) or len(cursor) > 200):
            raise ValueError("Unexpected instruments cursor")
    else:
        if set(result) != {"category", "symbol", "interval", "start", "end", "limit"}:
            raise ValueError("Unexpected kline parameters")
        if result["category"] != "linear" or result["interval"] != "60":
            raise ValueError("Unexpected kline parameters")
        symbol = result["symbol"]
        if not isinstance(symbol, str) or not 1 <= len(symbol) <= 24:
            raise ValueError("Unexpected kline symbol")
        if not symbol.isalnum() or not symbol.isupper():
            raise ValueError("Unexpected kline symbol")
        if result["limit"] != 1000:
            raise ValueError("Unexpected kline limit")
        for name in ("start", "end"):
            value = result[name]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("Invalid kline window")
        if result["start"] >= result["end"]:
            raise ValueError("Invalid kline window")
    return result


# Python 标准库源码，经 SSH 输入到 NAS 主机执行；只读公开 GET 由本地白名单约束。
_REMOTE = r"""
import datetime as dt
import json
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

jobs = __JOBS__
INTERVAL = 0.08

lock = threading.Lock()
state = {"next": 0.0}

def throttle():
    # 整批请求按固定间隔铺开，避免撞上交易所的按窗口限频。
    with lock:
        now = time.monotonic()
        wait = state["next"] - now
        if wait > 0:
            time.sleep(wait)
            now = time.monotonic()
        state["next"] = now + INTERVAL

def fetch(job):
    path, params = job
    last = ""
    for attempt in range(6):
        throttle()
        try:
            url = "https://api.bybit.com" + path + "?" + urllib.parse.urlencode(params)
            with urllib.request.urlopen(url, timeout=25) as response:
                payload = json.load(response)
            if payload.get("retCode") != 0:
                code = payload.get("retCode")
                if code == 10006:
                    # 命中按窗口限频：等窗口重置再试。
                    time.sleep(5.0)
                    raise ValueError("rate limited")
                raise ValueError("retCode=" + str(code))
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
with ThreadPoolExecutor(max_workers=3) as pool:
    records = list(pool.map(fetch, jobs))
print(json.dumps({"startedUtc": started,
                  "completedUtc": dt.datetime.now(dt.timezone.utc).isoformat(),
                  "records": records}, separators=(",", ":")))
"""


def fetch_public_many(
    jobs: Sequence[tuple[str, dict[str, Any]]],
    *,
    ssh_host: str = "nas",
    timeout: int = 600,
) -> dict[str, Any]:
    """一次 SSH 会话取多个公开 GET 响应；远端只用标准库。"""
    prepared = [(path, _allowed_request(path, params)) for path, params in jobs]
    if not prepared:
        raise ValueError("没有要取的公共请求")
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
        raise PublicDataError("公共响应信封格式不对")
    return envelope


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
        raise PublicDataError(f"公共响应缺失或重复：{path} {wanted}")
    if "error" in found[0]:
        raise PublicDataError(f"公共采集失败：{found[0]['error']}")
    payload = found[0]["payload"]
    if payload.get("retCode") != 0:
        raise PublicDataError(f"公共接口报错：{payload.get('retCode')}")
    return found


def _rows(envelope: Mapping[str, Any], path: str, params: Mapping[str, Any]) -> list:
    record = _records_for(envelope, path, params)[0]
    rows = record["payload"].get("result", {}).get("list")
    if not isinstance(rows, list):
        raise PublicDataError("公共响应缺少 list")
    return rows


def _next_cursor(envelope: Mapping[str, Any], path: str, params: Mapping[str, Any]) -> str | None:
    record = _records_for(envelope, path, params)[0]
    result = record["payload"].get("result", {})
    cursor = result.get("nextPageCursor")
    return cursor if isinstance(cursor, str) and cursor else None


def build_universe(
    fetch: Fetcher,
    *,
    top: int = DEFAULT_TOP_SYMBOLS,
) -> tuple[list[SymbolQuote], list[str]]:
    """按 24 小时成交额排序取前 ``top`` 个永续合约。

    返回合约列表与被跳过的符号（附原因），跳过项在报告里要留痕。
    """
    envelope = fetch([("/v5/market/tickers", {"category": "linear"})])
    tickers = _rows(envelope, "/v5/market/tickers", {"category": "linear"})
    turnover: dict[str, float] = {}
    for row in tickers:
        symbol = row.get("symbol")
        if not isinstance(symbol, str):
            continue
        try:
            turnover[symbol] = float(row.get("turnover24h") or 0.0)
        except (TypeError, ValueError):
            turnover[symbol] = 0.0

    instruments: dict[str, dict[str, Any]] = {}
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"category": "linear", "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        page = fetch([("/v5/market/instruments-info", params)])
        for row in _rows(page, "/v5/market/instruments-info", params):
            symbol = row.get("symbol")
            if isinstance(symbol, str):
                instruments[symbol] = row
        cursor = _next_cursor(page, "/v5/market/instruments-info", params)
        if not cursor:
            break

    quotes: list[SymbolQuote] = []
    skipped: list[str] = []
    for symbol, amount in sorted(turnover.items(), key=lambda item: item[1], reverse=True):
        spec = instruments.get(symbol)
        if spec is None:
            skipped.append(f"{symbol}: 没有合约规格")
            continue
        contract_type = str(spec.get("contractType") or "")
        status = str(spec.get("status") or "")
        if contract_type != "LinearPerpetual":
            skipped.append(f"{symbol}: 不是永续（{contract_type}）")
            continue
        if status.lower() != "trading":
            skipped.append(f"{symbol}: 状态不是 Trading（{status}）")
            continue
        quotes.append(
            SymbolQuote(
                symbol=symbol,
                turnover_24h=amount,
                contract_type=contract_type,
                status=status,
                base_coin=str(spec.get("baseCoin") or ""),
            )
        )
        if len(quotes) >= top:
            break
    return quotes, skipped


def _kline_pages(symbol: str, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
    """按页切出覆盖 ``[start, end)`` 的小时级 K 线请求参数。

    接口按 ``end`` 向前返回最多 ``limit`` 根（含 ``end`` 那一根），页与页在
    边界行重叠，加载时按时间戳去重。
    """
    pages: list[dict[str, Any]] = []
    boundary = end_ms
    while boundary > start_ms:
        pages.append(
            {
                "category": "linear",
                "symbol": symbol,
                "interval": "60",
                "start": start_ms,
                "end": boundary,
                "limit": 1000,
            }
        )
        if boundary - 1000 * HOUR_MS <= start_ms:
            break
        boundary -= 1000 * HOUR_MS
    return pages


def kline_jobs(
    symbols: Sequence[str], start_ms: int, end_ms: int
) -> list[tuple[str, dict[str, Any]]]:
    """构造全部小时级 K 线请求。"""
    jobs: list[tuple[str, dict[str, Any]]] = []
    for symbol in symbols:
        for params in _kline_pages(symbol, start_ms, end_ms):
            jobs.append(("/v5/market/kline", params))
    return jobs


def load_hourly_closes(
    envelope: Mapping[str, Any],
    symbols: Sequence[str],
    *,
    start_ms: int,
    end_ms: int,
) -> tuple[dict[str, dict[int, float]], list[str]]:
    """把 K 线响应整理成 ``{symbol: {时间戳: 收盘价}}``。

    未收盘的那一根会被丢弃；覆盖不足七成的符号直接剔除并留痕。
    """
    closes: dict[str, dict[int, float]] = {symbol: {} for symbol in symbols}
    for symbol in symbols:
        for params in _kline_pages(symbol, start_ms, end_ms):
            for row in _rows(envelope, "/v5/market/kline", params):
                if not isinstance(row, list) or len(row) < 5:
                    raise PublicDataError("K 线行格式不对")
                start = int(row[0])
                if start < start_ms or start >= end_ms:
                    continue
                if start + HOUR_MS > end_ms:
                    # 未收盘的那一根不参与统计。
                    continue
                try:
                    close = float(row[4])
                except (TypeError, ValueError):
                    continue
                if close > 0:
                    closes[symbol][start] = close
    expected = max(1, (end_ms - start_ms) // HOUR_MS)
    usable: dict[str, dict[int, float]] = {}
    dropped: list[str] = []
    for symbol, series in closes.items():
        if len(series) < expected * 0.7:
            dropped.append(f"{symbol}: 只有 {len(series)}/{expected} 根小时线，覆盖不足")
            continue
        usable[symbol] = series
    return usable, dropped


def aligned_log_prices(
    series_a: Mapping[int, float], series_b: Mapping[int, float]
) -> tuple[list[float], list[float]]:
    """取两条序列共有的时间戳，返回对齐后的对数价格。"""
    common = sorted(set(series_a) & set(series_b))
    if not common:
        return [], []
    return (
        [math.log(series_a[stamp]) for stamp in common],
        [math.log(series_b[stamp]) for stamp in common],
    )


def evaluate_pool(
    quotes: Sequence[SymbolQuote],
    closes: Mapping[str, Mapping[int, float]],
    *,
    configured_pairs: Sequence[str],
    min_observations: int = DEFAULT_MIN_OBSERVATIONS,
    adf_lag: int | None = 2,
    max_lag: int = 12,
    max_nominees: int = 10,
) -> tuple[list[PairResult], list[str], int]:
    """对池内所有配对做协整评估并归档分类。

    返回（保留的配对结果、跳过的配对说明、通过检验但未提名的配对数量）。
    """
    symbols = [quote.symbol for quote in quotes]
    base_coins = {quote.symbol: quote.base_coin for quote in quotes}
    configured = {tuple(pair.split(":")) for pair in configured_pairs if ":" in pair}
    in_book: list[PairResult] = []
    nominees: list[PairResult] = []
    skipped: list[str] = []
    for index, symbol_a in enumerate(symbols):
        for symbol_b in symbols[index + 1 :]:
            if base_coins.get(symbol_a) and base_coins.get(symbol_a) == base_coins.get(symbol_b):
                skipped.append(
                    f"{symbol_a}:{symbol_b}: 同一标的（{base_coins.get(symbol_a)}）的不同合约"
                )
                continue
            series_a, series_b = aligned_log_prices(closes[symbol_a], closes[symbol_b])
            if len(series_a) < min_observations:
                skipped.append(
                    f"{symbol_a}:{symbol_b}: 共同样本 {len(series_a)} 条，不足 {min_observations}"
                )
                continue
            evaluation = evaluate_pair(
                symbol_a,
                series_a,
                symbol_b,
                series_b,
                lag=adf_lag,
                max_lag=max_lag,
                span_hours=float(len(series_a)),
            )
            qualified = evaluation.stable and evaluation.both_integration_order_one
            result = PairResult(evaluation=evaluation, classification="ignore")
            if (symbol_a, symbol_b) in configured or (symbol_b, symbol_a) in configured:
                in_book.append(
                    PairResult(
                        evaluation=evaluation,
                        classification="keep" if qualified else "drop",
                    )
                )
            elif qualified:
                nominees.append(result)
    nominees.sort(
        key=lambda item: (
            item.evaluation.cointegration.p_value,
            item.evaluation.cointegration.half_life or float("inf"),
        )
    )
    kept_nominees = [
        PairResult(evaluation=item.evaluation, classification="nominate")
        for item in nominees[:max_nominees]
    ]
    others = len(nominees) - len(kept_nominees)
    return in_book + kept_nominees, skipped, others


def negative_beta(results: Sequence[PairResult]) -> list[PairResult]:
    """挑出协整系数为负的提名。

    当前配置模型要求 ``hedge_ratio > 0``（对数价差按 ``lnA - β·lnB`` 口径），
    负 β 的反向关系没法直接落到配置里，需要单独决定建模方式。
    """
    return [
        item
        for item in results
        if item.classification == "nominate"
        and item.evaluation.cointegration.hedge_ratio <= 0
    ]


def atomic_json(path: pathlib.Path, value: object) -> None:
    """原子写 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        pathlib.Path(temporary).replace(path)
    except BaseException:
        pathlib.Path(temporary).unlink(missing_ok=True)
        raise
