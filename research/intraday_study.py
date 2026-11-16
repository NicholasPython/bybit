#!/usr/bin/env python3
"""小时级"快交易"研究：在 Bybit 现货 1 小时 K 线上检验高换手策略能否覆盖成本。

这是**事后探索性**研究，不在 research/protocol.md 的预先检验范围内，结论强度弱于
主回测；它的作用是回答"更快交易是否更划算"，而不是挑选出一条可上线的策略。

用法::

    python research/intraday_study.py --data research/data/1h --out research/results
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import pathlib
import statistics
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from backtest import (  # noqa: E402
    Cost,
    Panel,
    StrategySet,
    bar_str,
    bootstrap_mean_ci,
    day_str,
    metrics,
    periodic_returns,
    run_backtest,
)

HOUR_MS = 3_600_000
PERIODS_YEAR = 365 * 24
#: 参与交易前需要的历史长度（小时，约 30 天）。
MIN_BARS = 24 * 30


def load_intraday(data_dir: pathlib.Path) -> Panel:
    """读取 1 小时 K 线，构造与主引擎兼容的面板。"""
    series: dict[str, dict[int, tuple[float, float, float]]] = {}
    for path in sorted((data_dir / "klines").glob("*.csv")):
        rows: dict[int, tuple[float, float, float]] = {}
        with path.open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                stamp = int(row["timestamp"])
                o, h, low, c = (float(row[key]) for key in ("open", "high", "low", "close"))
                volume, turnover = float(row["volume"]), float(row["turnover"])
                if (
                    stamp % HOUR_MS
                    or stamp in rows
                    or not all(math.isfinite(x) for x in (o, h, low, c, volume, turnover))
                    or min(o, h, low, c) <= 0
                    or low > min(o, c)
                    or h < max(o, c)
                    or volume < 0
                    or turnover < 0
                ):
                    raise ValueError(f"Invalid hourly candle: {path.name} {stamp}")
                rows[stamp] = (o, c, turnover)
        if len(rows) >= MIN_BARS:
            series[path.stem] = rows
    if not series:
        raise SystemExit("没有小时级数据")
    first = min(min(rows) for rows in series.values())
    last = max(max(rows) for rows in series.values())
    dates = list(range(first, last + 1, HOUR_MS))
    n = len(dates)
    symbols = sorted(series)
    closes, opens, masks, vols = [], [], [], []
    eligible: list[list[int]] = [[] for _ in dates]
    for k, symbol in enumerate(symbols):
        rows = series[symbol]
        c_row, o_row, mask, v_row = [], [], [], [0.0] * n
        age, last_close, turnover = 0, 0.0, []
        for i, stamp in enumerate(dates):
            hit = rows.get(stamp)
            mask.append(hit is not None)
            if hit is None:
                c_row.append(last_close)
                o_row.append(last_close)
                turnover.append(0.0)
                age = 0
            else:
                o, c, turn = hit
                o_row.append(o)
                c_row.append(c)
                turnover.append(turn)
                last_close = c
                age += 1
            if age >= 21:
                chunk = c_row[i - 20 : i + 1]
                rets = [math.log(b / a) for a, b in zip(chunk, chunk[1:], strict=False)]
                v_row[i] = statistics.stdev(rets) * math.sqrt(PERIODS_YEAR)
            if (
                age >= MIN_BARS
                and v_row[i] > 0.02
                and statistics.fmean(turnover[i - MIN_BARS + 1 : i + 1]) * 24 >= 1_000_000
            ):
                eligible[i].append(k)
        closes.append(c_row)
        opens.append(o_row)
        masks.append(mask)
        vols.append(v_row)
    return Panel(dates, symbols, closes, opens, masks, vols, eligible, PERIODS_YEAR)


def main() -> None:
    parser = argparse.ArgumentParser(description="小时级快交易研究")
    parser.add_argument("--data", default="research/data/1h")
    parser.add_argument("--out", default="research/results")
    parser.add_argument("--fee", type=float, default=0.001)
    parser.add_argument("--slippage", type=float, default=0.0005)
    args = parser.parse_args()

    panel = load_intraday(pathlib.Path(args.data))
    strategy = StrategySet(panel)
    cost = Cost(fee=args.fee, slippage=args.slippage)
    end = panel.n_dates - 1
    print(
        f"小时面板：{len(panel.symbols)} 个标的，{panel.n_dates} 根 "
        f"({day_str(panel.dates[0])} ~ {day_str(panel.dates[-1])})",
        flush=True,
    )

    rows: dict[str, dict] = {
        "study": {
            "exploratory": True,
            "periods_per_year": PERIODS_YEAR,
            "universe_selection": "Top 40 by turnover at the 2026-11-01 snapshot; hindsight bias",
            "symbols": panel.symbols,
            "start_open": bar_str(panel.dates[0], PERIODS_YEAR),
            "end_open": bar_str(panel.dates[-1], PERIODS_YEAR),
            "min_30d_mean_daily_turnover_usdt": 1_000_000,
            "cost": {"fee": cost.fee, "slippage": cost.slippage},
        },
    }
    benchmarks = {
        "buy_and_hold_btc": {"family": "buy_hold", "symbol": "BTCUSDT"},
    }
    for name, params in benchmarks.items():
        result = run_backtest(panel, strategy.make(params["family"], params), cost, 0, end, name)
        rows[name] = {"params": params, **metrics(result), "yearly": periodic_returns(result, 4)}
        rows[name]["daily_returns"] = result.daily_returns

    grid = []
    for lookback in (6, 12, 24, 48):
        for z in (1.5, 2.0, 2.5):
            for hold in (6, 12, 24):
                grid.append(
                    {
                        "family": "reversion",
                        "lookback": lookback,
                        "z_threshold": z,
                        "hold_days": hold,
                        "per_trade": 0.05,
                    }
                )
    results = []
    for params in grid:
        result = run_backtest(
            panel, strategy.make("reversion", params), cost, 0, end, "intraday-reversion"
        )
        results.append((params, result, metrics(result)))
    best = max(results, key=lambda item: (item[2]["sharpe"], item[2]["max_drawdown"]))
    sharpes = [m["sharpe"] for _, _, m in results]
    rows["intraday_reversion_best"] = {
        "params": best[0],
        **best[2],
        "yearly": periodic_returns(best[1], 4),
        "grid_size": len(grid),
        "grid_sharpe_distribution": {
            "min": min(sharpes),
            "median": statistics.median(sharpes),
            "max": max(sharpes),
            "mean": statistics.fmean(sharpes),
        },
        "grid_positive_ratio": sum(1 for s in sharpes if s > 0) / len(sharpes),
        "bootstrap": bootstrap_mean_ci(
            best[1].daily_returns, block=48, periods_per_year=PERIODS_YEAR
        ),
    }
    rows["intraday_reversion_all"] = [
        {
            "params": p,
            "sharpe": m["sharpe"],
            "cagr": m["cagr"],
            "max_drawdown": m["max_drawdown"],
            "turnover_per_year": m["turnover_per_year"],
        }
        for p, _, m in sorted(results, key=lambda item: -item[2]["sharpe"])
    ]

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "intraday_results.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    for name, row in rows.items():
        if name in ("intraday_reversion_all", "study"):
            continue
        print(
            f"{name}: 年化{row.get('cagr', 0):.1%} 夏普{row.get('sharpe', 0):.2f} "
            f"回撤{row.get('max_drawdown', 0):.1%} 换手{row.get('turnover_per_year', 0):.1f}/年",
            flush=True,
        )
    print(f"结果已写入 {out / 'intraday_results.json'}", flush=True)


if __name__ == "__main__":
    main()
