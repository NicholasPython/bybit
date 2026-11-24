"""Publish the Top20 correlation snapshot from the collected minute candles.

The dashboard reads a fixed snapshot so that every reader sees the same
numbers. This script rebuilds it from the local SQLite database:

* hourly closes per coin, aggregated from the collected 1m candles
* hourly log returns r(t) = ln[C(t) / C(t-1)]
* Pearson and Spearman correlations over fixed 30 and 90 day windows
* the 24h USDT turnover that fixes the ranking order

Only public Bybit market data is used. Nothing here places an order.
"""

import argparse
import csv
import datetime as dt
import io
import json
import math
import sqlite3
import zipfile
from pathlib import Path

from market_config import INSTRUMENTS

HOUR_MS = 3_600_000
DAY_MS = 86_400_000
WINDOW_DAYS = (30, 90)
# The common short window covers the coins with the shortest listed history.
COMMON_WINDOW_HOURS = 161
BAR_MS = 60_000


def base_asset(symbol):
    return symbol[:-4] if symbol.endswith("USDT") else symbol


def cst(ms):
    return (
        dt.datetime.fromtimestamp(ms / 1000, dt.UTC)
        .astimezone(dt.timezone(dt.timedelta(hours=8)))
        .strftime("%Y-%m-%d %H:%M")
    )


def hourly_closes(db):
    """Aggregate the 1m candles into hourly closes, last close of each hour."""
    rows = db.execute(
        """
        SELECT symbol, bucket, c FROM (
            SELECT symbol,
                   ts_ms - (ts_ms % 3600000) AS bucket,
                   c,
                   ROW_NUMBER() OVER (
                       PARTITION BY symbol, ts_ms - (ts_ms % 3600000)
                       ORDER BY ts_ms DESC
                   ) AS position
            FROM candles_1m
        ) WHERE position = 1
        """
    )
    closes = {symbol: {} for symbol in INSTRUMENTS}
    for symbol, bucket, close in rows:
        if symbol in closes:
            closes[symbol][bucket] = float(close)
    return closes


def hourly_returns(closes):
    """Hourly log returns r(t) = ln[C(t) / C(t-1)] keyed by the bar time."""
    returns = {}
    for symbol, series in closes.items():
        rows = {}
        stamps = sorted(series)
        for previous, current in zip(stamps, stamps[1:], strict=False):
            if current - previous != HOUR_MS:
                continue
            if series[previous] <= 0 or series[current] <= 0:
                continue
            rows[current] = math.log(series[current] / series[previous])
        returns[symbol] = rows
    return returns


def pearson(xs, ys):
    if len(xs) < 2:
        return None
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    dx = [x - mean_x for x in xs]
    dy = [y - mean_y for y in ys]
    denominator = math.sqrt(sum(a * a for a in dx) * sum(b * b for b in dy))
    if denominator == 0:
        return None
    return sum(a * b for a, b in zip(dx, dy, strict=False)) / denominator


def ranks(values):
    """Average ranks so that ties share a position."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    result = [0.0] * len(values)
    index = 0
    while index < len(order):
        stop = index
        while stop + 1 < len(order) and values[order[stop + 1]] == values[order[index]]:
            stop += 1
        average = (index + stop) / 2 + 1
        for position in range(index, stop + 1):
            result[order[position]] = average
        index = stop + 1
    return result


def spearman(xs, ys):
    if len(xs) < 2:
        return None
    return pearson(ranks(xs), ranks(ys))


def window_pairs(returns, symbols, start, end, min_samples=2):
    """Correlation of every ordered pair over the shared hours in the window.

    ``min_samples`` guards against publishing a coefficient computed from a
    handful of hours; such pairs stay null so the page can show "NA 历史不足".
    """
    matrix = [[None] * len(symbols) for _ in symbols]
    details = []
    for i, first in enumerate(symbols):
        left = returns[first]
        for j, second in enumerate(symbols):
            if j < i:
                continue
            right = returns[second]
            stamps = sorted(set(left) & set(right))
            stamps = [ts for ts in stamps if start < ts <= end]
            if len(stamps) < min_samples:
                continue
            xs = [left[ts] for ts in stamps]
            ys = [right[ts] for ts in stamps]
            coefficient = pearson(xs, ys)
            matrix[i][j] = coefficient
            matrix[j][i] = coefficient
            if j == i or coefficient is None:
                continue
            same = 0
            counted = 0
            for x, y in zip(xs, ys, strict=False):
                if x == 0 or y == 0:
                    continue
                counted += 1
                if (x > 0) == (y > 0):
                    same += 1
            details.append(
                {
                    "a": first,
                    "b": second,
                    "pearson": coefficient,
                    "spearman": spearman(xs, ys),
                    "samples": len(stamps),
                    "direction_agreement_pct": (same / counted * 100) if counted else None,
                }
            )
    return matrix, details


def rolling_range(returns, first, second, start, end):
    """Rolling 30 day correlation range, sampled once per day."""
    left = returns[first]
    right = returns[second]
    stamps = sorted(set(left) & set(right))
    stamps = [ts for ts in stamps if start < ts <= end]
    if len(stamps) < 30 * 24:
        return None, None
    values = []
    for offset in range(0, len(stamps) - 30 * 24 + 1, 24):
        window = stamps[offset : offset + 30 * 24]
        xs = [left[ts] for ts in window]
        ys = [right[ts] for ts in window]
        coefficient = pearson(xs, ys)
        if coefficient is not None:
            values.append(coefficient)
    if not values:
        return None, None
    return min(values), max(values)


def price_return(closes, symbol, start, end):
    series = closes[symbol]
    stamps = [ts for ts in sorted(series) if start < ts <= end]
    if len(stamps) < 2:
        return None
    first = series[stamps[0]]
    last = series[stamps[-1]]
    if first <= 0:
        return None
    return (last / first - 1) * 100


def fetch_turnover(client):
    """24h USDT turnover per symbol from the public ticker endpoint."""
    payload = client.get("/v5/market/tickers", {"category": "spot", "limit": 1000})
    turnover = {}
    for row in payload.get("result", {}).get("list", []):
        symbol = row.get("symbol")
        if symbol in INSTRUMENTS:
            turnover[symbol] = float(row.get("turnover24h") or 0)
    return turnover


def build_analysis(db, turnover):
    closes = hourly_closes(db)
    returns = hourly_returns(closes)
    end = min(max(stamps) for stamps in closes.values() if stamps)
    windows = {}
    for days in WINDOW_DAYS:
        windows[days] = (end - days * DAY_MS, end)

    ranking = []
    for symbol in INSTRUMENTS:
        series = closes[symbol]
        row = {
            "symbol": symbol,
            "base": base_asset(symbol),
            "volume_24h_usdt": turnover.get(symbol, 0.0),
            "hourly_candles": len(series),
        }
        for days in WINDOW_DAYS:
            start, stop = windows[days]
            samples = len([ts for ts in returns[symbol] if start < ts <= stop])
            row[f"samples_{days}d"] = samples
            row[f"complete_{days}d"] = samples >= days * 24
            row[f"return_{days}d_pct"] = price_return(closes, symbol, start, stop)
        ranking.append(row)
    ranking.sort(key=lambda item: item["volume_24h_usdt"], reverse=True)
    for position, row in enumerate(ranking, start=1):
        row["rank"] = position

    symbols = [row["symbol"] for row in ranking]
    matrices = {}
    pairs = []
    for days in WINDOW_DAYS:
        start, stop = windows[days]
        # A pair needs at least half the window's hours before a coefficient is
        # published; shorter overlap stays null so the page shows NA.
        minimum = max(2, days * 24 // 2)
        matrix, details = window_pairs(returns, symbols, start, stop, minimum)
        matrices[str(days)] = matrix
        for detail in details:
            detail["days"] = days
            if days == 90:
                low, high = rolling_range(returns, detail["a"], detail["b"], start, stop)
                detail["rolling30_min"] = low
                detail["rolling30_max"] = high
            else:
                detail["rolling30_min"] = None
                detail["rolling30_max"] = None
            pairs.append(detail)

    common_start = end - COMMON_WINDOW_HOURS * HOUR_MS
    common_matrix, _ = window_pairs(
        returns, symbols, common_start, end, max(2, COMMON_WINDOW_HOURS // 2)
    )
    common_samples = len(
        sorted(
            set.intersection(
                *[
                    {ts for ts in returns[symbol] if common_start < ts <= end}
                    for symbol in symbols
                ]
            )
        )
    )

    return {
        "coins": symbols,
        "ranking": ranking,
        "matrices": matrices,
        "pairs": pairs,
        "snapshot": {
            "snapshot_ms": end,
            "source": "Bybit V5 /v5/market/tickers",
            "universe": "USDT 现货，按 24 小时成交额排名，排除稳定币",
        },
        "analysis_end_ms": end,
        "analysis_end_cst": cst(end),
        "windows_cst": {
            str(days): [cst(start), cst(stop)] for days, (start, stop) in windows.items()
        },
        "method": (
            "小时收益 r(t) = ln[C(t) / C(t-1)]，取自已完结 1 小时 K 线；"
            "Pearson / Spearman 基于固定 30 天与 90 天窗口内双方共同存在的小时样本，"
            "缺失不填补；统计不含手续费与滑点。"
        ),
        "common_short_window": {
            "start_ms": common_start,
            "end_ms": end,
            "samples": common_samples,
            "pearson_matrix": common_matrix,
        },
        "exploratory_clusters_90d": [],
        "independent_validation": {
            "spearman_recomputed": True,
            "samples_min": min((p["samples"] for p in pairs), default=0),
        },
    }


def matrix_csv(matrix, symbols, days):
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["symbol"] + symbols)
    for index, symbol in enumerate(symbols):
        row = []
        for value in matrix[index]:
            row.append("" if value is None else f"{value:.6f}")
        writer.writerow([symbol] + row)
    return buffer.getvalue()


def ranking_csv(analysis):
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "rank",
            "symbol",
            "base",
            "volume_24h_usdt",
            "return_30d_pct",
            "return_90d_pct",
            "samples_30d",
            "samples_90d",
            "complete_30d",
            "complete_90d",
            "hourly_candles",
        ]
    )
    for row in analysis["ranking"]:
        writer.writerow(
            [
                row["rank"],
                row["symbol"],
                row["base"],
                f"{row['volume_24h_usdt']:.2f}",
                "" if row["return_30d_pct"] is None else f"{row['return_30d_pct']:.6f}",
                "" if row["return_90d_pct"] is None else f"{row['return_90d_pct']:.6f}",
                row["samples_30d"],
                row["samples_90d"],
                int(row["complete_30d"]),
                int(row["complete_90d"]),
                row["hourly_candles"],
            ]
        )
    return buffer.getvalue()


def pairs_csv(analysis):
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "a",
            "b",
            "days",
            "pearson",
            "spearman",
            "samples",
            "direction_agreement_pct",
            "rolling30_min",
            "rolling30_max",
        ]
    )
    for pair in sorted(analysis["pairs"], key=lambda p: (p["days"], p["a"], p["b"])):
        writer.writerow(
            [
                pair["a"],
                pair["b"],
                pair["days"],
                "" if pair["pearson"] is None else f"{pair['pearson']:.6f}",
                "" if pair["spearman"] is None else f"{pair['spearman']:.6f}",
                pair["samples"],
                ""
                if pair["direction_agreement_pct"] is None
                else f"{pair['direction_agreement_pct']:.4f}",
                "" if pair["rolling30_min"] is None else f"{pair['rolling30_min']:.6f}",
                "" if pair["rolling30_max"] is None else f"{pair['rolling30_max']:.6f}",
            ]
        )
    return buffer.getvalue()


def report_md(analysis):
    lines = [
        "# Bybit 现货 Top20 收益相关性报告",
        "",
        f"- 排名快照：{cst(analysis['snapshot']['snapshot_ms'])}",
        f"- 数据截至：{analysis['analysis_end_cst']}",
        f"- 方法：{analysis['method']}",
        "",
        "## 热门现货 Top20",
        "",
        "| 排名 | 币种 | 24h 成交额 (USDT) | 30 天收益 | 90 天收益 | 90 天样本 |",
        "| ---: | --- | ---: | ---: | ---: | ---: |",
    ]
    for row in analysis["ranking"]:
        ret30 = "—" if row["return_30d_pct"] is None else f"{row['return_30d_pct']:.2f}%"
        ret90 = "—" if row["return_90d_pct"] is None else f"{row['return_90d_pct']:.2f}%"
        lines.append(
            f"| {row['rank']} | {row['base']} | {row['volume_24h_usdt']:,.0f} "
            f"| {ret30} | {ret90} | {row['samples_90d']} |"
        )
    for days in WINDOW_DAYS:
        lines += ["", f"## {days} 天窗口：同向关联最高与最低的币对", ""]
        pairs = [p for p in analysis["pairs"] if p["days"] == days and p["pearson"] is not None]
        for title, subset in (
            ("最高", sorted(pairs, key=lambda p: -p["pearson"])[:5]),
            ("最低", sorted(pairs, key=lambda p: p["pearson"])[:5]),
        ):
            lines.append(f"- **{title}**")
            for pair in subset:
                spearman = pair["spearman"]
                spearman_text = "—" if spearman is None else f"{spearman:.3f}"
                lines.append(
                    f"  - {base_asset(pair['a'])} / {base_asset(pair['b'])}: "
                    f"Pearson {pair['pearson']:.3f}，Spearman {spearman_text}"
                    f"，样本 {pair['samples']}"
                )
    lines += [
        "",
        "## 说明",
        "",
        "相关性衡量共同波动，不等于累计收益相同，也不代表再平衡可以盈利。",
        "本报告只使用公开行情数据，不含任何交易凭据，也不下单。",
        "",
    ]
    return "\n".join(lines)


def publish(directory, analysis):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "analysis.json").write_text(
        json.dumps(analysis, ensure_ascii=False, allow_nan=False), encoding="utf-8"
    )
    symbols = analysis["coins"]
    (directory / "top20-ranking.csv").write_text(ranking_csv(analysis), encoding="utf-8")
    (directory / "pair-correlations.csv").write_text(pairs_csv(analysis), encoding="utf-8")
    for days in WINDOW_DAYS:
        (directory / f"correlation-{days}.csv").write_text(
            matrix_csv(analysis["matrices"][str(days)], symbols, days), encoding="utf-8"
        )
    (directory / "correlation-common-161h.csv").write_text(
        matrix_csv(
            analysis["common_short_window"]["pearson_matrix"],
            symbols,
            COMMON_WINDOW_HOURS,
        ),
        encoding="utf-8",
    )
    (directory / "report.md").write_text(report_md(analysis), encoding="utf-8")
    with zipfile.ZipFile(
        directory / "bybit-top20-analysis.zip", "w", zipfile.ZIP_DEFLATED
    ) as archive:
        for name in (
            "analysis.json",
            "top20-ranking.csv",
            "pair-correlations.csv",
            "correlation-30.csv",
            "correlation-90.csv",
            "correlation-common-161h.csv",
            "report.md",
        ):
            archive.write(directory / name, arcname=name)
    return directory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--turnover-json", type=Path, default=None)
    args = parser.parse_args()

    if args.turnover_json:
        turnover = json.loads(args.turnover_json.read_text(encoding="utf-8"))
    else:
        from collector import BybitPublic

        client = BybitPublic()
        try:
            turnover = fetch_turnover(client)
        finally:
            client.close()
    db = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True, timeout=30)
    try:
        analysis = build_analysis(db, turnover)
    finally:
        db.close()
    publish(args.out, analysis)
    print(
        f"published {args.out} coins={len(analysis['coins'])} "
        f"end={analysis['analysis_end_cst']} pairs={len(analysis['pairs'])}"
    )


if __name__ == "__main__":
    main()
