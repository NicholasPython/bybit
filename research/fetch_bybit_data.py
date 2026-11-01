#!/usr/bin/env python3
"""从 Bybit 公开行情接口抓取策略研究用的历史数据。

数据全部来自公开只读接口，不携带任何密钥。抓取结果写入 --out 目录：

- ``instruments_spot.json``  现货合约元数据（不提供 launchTime）
- ``klines/<symbol>.csv``    现货 K 线
- ``hv/<base>_<period>.csv``  标的历史波动率 HV（不是期权隐含波动率 IV）
- ``options_instruments_<base>.json``  期权合约元数据

用法（在能访问 Bybit 的机器上）::

    python fetch_bybit_data.py --out /out --proxy http://127.0.0.1:7890
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import pathlib
import time
from typing import Any

import httpx

BASE_URL = "https://api.bybit.com"
#: 研究主区间起点（含），此前无 Bybit 现货数据的年份不纳入。
GLOBAL_START = "2021-01-01"
#: 标的池固定日：只有在此之前上市的现货对才进入样本，避免用事后信息挑币。
UNIVERSE_CUTOFF = "2024-01-01"
#: 稳定币兑稳定币与法币对不作为趋势/反转研究标的。
EXCLUDED_BASES = {
    "USDC",
    "FDUSD",
    "TUSD",
    "EURT",
    "USDE",
    "DAI",
    "EURI",
    "XUSD",
    "EUR",
    "USD1",
    "BUSD",
}


def _utc_ms(day: str) -> int:
    """把 YYYY-MM-DD 转成 UTC 毫秒时间戳。"""
    moment = dt.datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=dt.UTC)
    return int(moment.timestamp() * 1000)


def _sleep(attempt: int) -> None:
    time.sleep(1.0 + 1.5 * attempt)


def get_result(
    client: httpx.Client,
    path: str,
    params: dict[str, Any] | None = None,
    attempts: int = 5,
) -> dict[str, Any] | list[dict[str, Any]]:
    """调用只读接口并返回 result 字段，失败时按退避重试。"""
    last = ""
    for attempt in range(attempts):
        try:
            response = client.get(BASE_URL + path, params=params)
            if response.status_code == 200:
                payload = response.json()
                if payload.get("retCode") == 0:
                    return payload["result"]
                last = f"retCode={payload.get('retCode')} {payload.get('retMsg')}"
            else:
                last = f"http={response.status_code}"
        except Exception as exc:  # noqa: BLE001 - 网络错误统一退避重试
            last = f"{type(exc).__name__}: {exc}"
        _sleep(attempt)
    raise RuntimeError(f"{path} params={params} failed after {attempts} tries: {last}")


def fetch_instruments(
    client: httpx.Client, category: str, base_coin: str | None = None
) -> list[dict[str, Any]]:
    """分页拉取合约元数据。"""
    items: list[dict[str, Any]] = []
    cursor = ""
    while True:
        params: dict[str, Any] = {"category": category, "limit": 1000}
        if base_coin:
            params["baseCoin"] = base_coin
        if cursor:
            params["cursor"] = cursor
        result = get_result(client, "/v5/market/instruments-info", params)
        batch = result.get("list") or []
        items.extend(batch)
        cursor = result.get("nextPageCursor") or ""
        if not batch or not cursor:
            break
    return items


def fetch_klines(
    client: httpx.Client,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    limit: int = 1000,
) -> list[list[str]]:
    """按时间顺序返回 K 线行。

    接口在给定区间内只返回**最近的** limit 根，因此分页必须把区间右端往前挪，
    否则只能拿到最近 1000 根（两年多的日线就断档了）。
    """
    rows: list[list[str]] = []
    seen: set[str] = set()
    cursor_end = end_ms
    while cursor_end > start_ms:
        result = get_result(
            client,
            "/v5/market/kline",
            {
                "category": "spot",
                "symbol": symbol,
                "interval": interval,
                "start": start_ms,
                "end": cursor_end,
                "limit": limit,
            },
        )
        batch = result.get("list") or []
        if not batch:
            break
        fresh = [row for row in batch if row[0] not in seen]
        if not fresh:
            break
        for row in fresh:
            seen.add(row[0])
            rows.append(row)
        oldest = min(int(row[0]) for row in fresh)
        if oldest <= start_ms:
            break
        cursor_end = oldest - 1
    rows.sort(key=lambda row: int(row[0]))
    return rows


def fetch_hv(
    client: httpx.Client,
    base_coin: str,
    period: int,
    start_ms: int,
    end_ms: int,
) -> list[list[str]]:
    """拉取标的历史波动率 HV。USDT 数据须显式指定报价币；窗口上限 30 天。"""
    unique: dict[int, list[str]] = {}
    window = 30 * 24 * 3600 * 1000
    cursor = start_ms
    while cursor <= end_ms:
        stop = min(cursor + window, end_ms)
        result = get_result(
            client,
            "/v5/market/historical-volatility",
            {
                "category": "option",
                "baseCoin": base_coin,
                "quoteCoin": "USDT",
                "period": period,
                "startTime": cursor,
                "endTime": stop,
            },
        )
        if not isinstance(result, list):
            raise ValueError("Unexpected historical-volatility response")
        for row in result:
            stamp = int(row["time"])
            unique[stamp] = [str(stamp), str(row["value"]), str(row["period"])]
        cursor = stop + 1
        time.sleep(0.15)
    return [unique[key] for key in sorted(unique)]


def write_csv(path: pathlib.Path, header: list[str], rows: list[list[Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="抓取 Bybit 研究数据")
    parser.add_argument("--out", required=True, help="输出目录")
    parser.add_argument("--proxy", default="", help="HTTP 代理，例如 http://127.0.0.1:7890")
    parser.add_argument("--start", default=GLOBAL_START, help="K 线起点 YYYY-MM-DD")
    parser.add_argument("--universe-cutoff", default=UNIVERSE_CUTOFF, help="标的池固定日")
    parser.add_argument("--hv-base", default="BTC,ETH", help="历史波动率 HV 的标的币")
    parser.add_argument("--max-symbols", type=int, default=0, help="0 表示不限制")
    parser.add_argument("--interval", default="D", help="K 线周期，例如 D 或 60")
    parser.add_argument("--symbols-file", default="", help="只抓取列表文件里的标的")
    parser.add_argument("--tag", default="", help="输出子目录名，例如 1h")
    args = parser.parse_args()

    out = pathlib.Path(args.out)
    if args.tag:
        out = out / args.tag
    out.mkdir(parents=True, exist_ok=True)
    start_ms = _utc_ms(args.start)
    end_ms = int(time.time() * 1000)
    cutoff_ms = _utc_ms(args.universe_cutoff)

    with httpx.Client(
        proxy=args.proxy or None,
        timeout=30.0,
        trust_env=False,
        headers={"Accept": "application/json"},
    ) as client:
        instruments = fetch_instruments(client, "spot")
        (out / "instruments_spot.json").write_text(
            json.dumps(instruments, ensure_ascii=False), encoding="utf-8"
        )

        universe = [
            item
            for item in instruments
            if item.get("quoteCoin") == "USDT"
            and item.get("status") == "Trading"
            and item.get("baseCoin") not in EXCLUDED_BASES
            and int(item.get("launchTime") or 0) <= cutoff_ms
        ]
        if args.symbols_file:
            wanted = {
                line.strip()
                for line in pathlib.Path(args.symbols_file).read_text().splitlines()
                if line.strip()
            }
            universe = [item for item in universe if item["symbol"] in wanted]
        universe.sort(key=lambda item: item["symbol"])
        if args.max_symbols:
            universe = universe[: args.max_symbols]
        print(f"universe={len(universe)}", flush=True)

        for index, item in enumerate(universe, start=1):
            symbol = item["symbol"]
            launched = int(item.get("launchTime") or start_ms)
            begin = max(start_ms, launched)
            try:
                rows = fetch_klines(client, symbol, args.interval, begin, end_ms)
            except RuntimeError as exc:
                print(f"skip {symbol}: {exc}", flush=True)
                continue
            write_csv(
                out / "klines" / f"{symbol}.csv",
                ["timestamp", "open", "high", "low", "close", "volume", "turnover"],
                rows,
            )
            if index % 10 == 0 or index == len(universe):
                print(f"klines {index}/{len(universe)}", flush=True)

        for base in [item.strip().upper() for item in args.hv_base.split(",") if item.strip()]:
            for period in (7, 30):
                try:
                    rows = fetch_hv(
                        client,
                        base,
                        period,
                        end_ms - 730 * 24 * 3600 * 1000,
                        end_ms,
                    )
                except RuntimeError as exc:
                    print(f"skip hv {base} {period}: {exc}", flush=True)
                    continue
                write_csv(
                    out / "hv" / f"{base}_{period}.csv",
                    ["timestamp", "historical_volatility", "period_days"],
                    rows,
                )
                print(f"hv {base} {period}: {len(rows)}", flush=True)

            try:
                options = fetch_instruments(client, "option", base)
            except RuntimeError as exc:
                print(f"skip options {base}: {exc}", flush=True)
                continue
            (out / f"options_instruments_{base}.json").write_text(
                json.dumps(options, ensure_ascii=False), encoding="utf-8"
            )
            print(f"options {base}: {len(options)}", flush=True)

    manifest: list[tuple[str, str, str, int]] = []
    (out / "FETCHED_AT.txt").write_text(
        dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), encoding="utf-8"
    )
    for path in sorted((out / "klines").glob("*.csv")):
        with path.open(encoding="utf-8") as handle:
            rows = list(csv.reader(handle))
        if len(rows) < 2:
            continue
        manifest.append((path.stem, rows[1][0], rows[-1][0], len(rows) - 1))
    write_csv(out / "manifest.csv", ["symbol", "first_ts", "last_ts", "rows"], manifest)
    print("done", flush=True)


if __name__ == "__main__":
    main()
