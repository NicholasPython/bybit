#!/usr/bin/env python3
"""按 research/protocol.md 执行样本内/样本外与滚动前进回测，输出 JSON 结果。

用法::

    python research/run_research.py --data research/data --out research/results
"""

from __future__ import annotations

import argparse
import json
import pathlib
import statistics
import sys
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from backtest import (  # noqa: E402
    Backtest,
    Cost,
    Panel,
    StrategySet,
    bootstrap_mean_ci,
    day_str,
    deflated_sharpe,
    load_panel,
    metrics,
    periodic_returns,
    run_backtest,
)

IS_END = "2023-12-31"
OOS_START = "2024-01-01"
WF_TRAIN = 730
WF_STEP = 180


def tsmom_grid(full: bool) -> list[dict[str, Any]]:
    lookbacks = (20, 40, 60, 90, 120, 180, 200) if full else (40, 90, 180)
    rebalances = (1, 5, 20) if full else (5, 20)
    return [
        {"family": "tsmom", "lookback": lb, "rebalance": rb, "target_vol": tv, "market_filter": mf}
        for lb in lookbacks
        for rb in rebalances
        for tv in (0.3, 0.5)
        for mf in (True, False)
    ]


def cross_grid(full: bool) -> list[dict[str, Any]]:
    lookbacks = (14, 30, 60, 90) if full else (30, 60)
    tops = (3, 5, 10) if full else (5,)
    return [
        {"family": "cross", "lookback": lb, "top_k": k, "rebalance": rb, "target_vol": tv}
        for lb in lookbacks
        for k in tops
        for rb in (5, 20)
        for tv in (0.3, 0.5)
    ]


def reversion_grid(full: bool) -> list[dict[str, Any]]:
    lookbacks = (3, 5, 10) if full else (3, 5)
    return [
        {"family": "reversion", "lookback": lb, "z_threshold": z, "hold_days": h}
        for lb in lookbacks
        for z in (1.5, 2.0, 2.5)
        for h in (3, 5, 10)
    ]


GRIDS = {"tsmom": tsmom_grid, "cross": cross_grid, "reversion": reversion_grid}


def run_one(
    panel: Panel,
    strategy: StrategySet,
    params: dict[str, Any],
    cost: Cost,
    start: int,
    end: int,
    name: str,
):
    return run_backtest(panel, strategy.make(params["family"], params), cost, start, end, name)


def evaluate_grid(
    panel: Panel,
    strategy: StrategySet,
    grid: list[dict[str, Any]],
    cost: Cost,
    start: int,
    end: int,
) -> list[dict[str, Any]]:
    rows = []
    for params in grid:
        result = run_one(panel, strategy, params, cost, start, end, "grid")
        row = {"params": params, **metrics(result)}
        row["yearly"] = periodic_returns(result, 4)
        row["equity"] = result.equity
        row["daily_returns"] = result.daily_returns
        rows.append(row)
    return rows


def walk_forward(
    panel: Panel,
    strategy: StrategySet,
    grid: list[dict[str, Any]],
    cost: Cost,
    start: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Select using completed training windows, then trade one continuous portfolio.

    Training marks at open(i-1), before the application's open(i). Parameter changes
    rebalance real holdings; a missing quote keeps quantities and blocks execution.
    The final partial window is included and the final exit needs a real quote.
    """
    n = panel.n_dates
    segments: list[dict[str, Any]] = []
    schedules = []
    i = start
    while i < n - 1:
        train_end = i - 1
        train_start = max(0, train_end - WF_TRAIN)
        apply_end = min(i + WF_STEP, n - 1)
        best_params, best_key, best_row = None, None, None
        for params in grid:
            result = run_backtest(
                panel,
                strategy.make(params["family"], params),
                cost,
                train_start,
                train_end,
                "wf-train",
                liquidate_at_end=False,
            )
            row = metrics(result)
            key = (row["sharpe"], row["max_drawdown"])
            if best_key is None or key > best_key:
                best_key, best_params, best_row = key, params, row
        schedules.append(
            (
                i,
                apply_end,
                strategy.make(
                    best_params["family"],
                    best_params,
                    force_initial=True,
                ),
            )
        )
        segments.append(
            {
                "train_start": day_str(panel.dates[train_start]),
                "train_end": day_str(panel.dates[train_end]),
                "apply_start": day_str(panel.dates[i]),
                "apply_end": day_str(panel.dates[apply_end]),
                "params": best_params,
                "train_sharpe": best_row["sharpe"],
            }
        )
        i = apply_end

    active = 0

    def weights(i: int, nav: float):
        nonlocal active
        while active + 1 < len(schedules) and i >= schedules[active][1]:
            active += 1
        return schedules[active][2](i, nav)

    def on_execution(accepted: bool) -> None:
        schedules[active][2].on_execution(accepted)

    weights.on_execution = on_execution
    joined = run_backtest(panel, weights, cost, start, n - 1, "walk-forward")
    for (begin, end, _), segment in zip(schedules, segments, strict=True):
        lo, hi = begin - start, end - start
        base = joined.equity[lo]
        block = Backtest(
            "wf-apply",
            joined.daily_returns[lo:hi],
            [v / base for v in joined.equity[lo : hi + 1]],
            joined.dates[lo:hi],
            joined.turnover[lo:hi],
            joined.exposure[lo:hi],
            periods_per_year=panel.periods_per_year,
        )
        segment.update(metrics(block))
        segment.pop("trades")  # Trade count is recorded for the continuous run only.
    summary = metrics(joined)
    summary.update(
        {
            "yearly": periodic_returns(joined, 4),
            "equity": joined.equity,
            "dates": joined.dates,
            "daily_returns": joined.daily_returns,
            "segments": len(segments),
            "execution": joined.meta,
        }
    )
    return summary, segments


def main() -> None:
    parser = argparse.ArgumentParser(description="执行 Bybit 数据的策略研究回测")
    parser.add_argument("--data", default="research/data")
    parser.add_argument("--out", default="research/results")
    parser.add_argument("--fee", type=float, default=0.001)
    parser.add_argument("--slippage", type=float, default=0.0005)
    parser.add_argument("--families", default="tsmom,cross,reversion")
    parser.add_argument("--as-of", default="2026-11-01")
    parser.add_argument("--universe-cutoff", default="2024-01-01")
    args = parser.parse_args()

    data_dir, out_dir = pathlib.Path(args.data), pathlib.Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    cost = Cost(fee=args.fee, slippage=args.slippage)

    panel = load_panel(data_dir, universe_cutoff=args.universe_cutoff, as_of=args.as_of)
    strategy = StrategySet(panel)
    # IS 段右端取 IS_END 当天开盘作为最后一次定价，样本外从 OOS_START 的开盘开始；
    # 两者之间不留重叠，IS 的选参看不到 OOS 的任何成交价。
    is_end = panel.day_index(IS_END)
    oos_start = panel.day_index(OOS_START)
    wf_start = panel.day_index("2023-07-06")
    # 末端边界是平仓日，必须留出一天；最后一根日线的收盘价不参与定价。
    full_end = panel.n_dates - 1
    print(
        f"面板：{len(panel.symbols)} 个标的，{panel.n_dates} 天 "
        f"({panel.dates[0]} ~ {panel.dates[-1]})；IS 结束={is_end}，OOS 起点={oos_start}",
        flush=True,
    )

    report: dict[str, Any] = {
        "audit": panel.audit,
        "cost": {"fee": args.fee, "slippage": args.slippage},
        "split": {
            "is_end": IS_END,
            "oos_start": OOS_START,
            "wf_train": WF_TRAIN,
            "wf_step": WF_STEP,
        },
        "benchmarks": {},
        "families": {},
    }

    benchmarks = {
        "buy_and_hold_btc": {"family": "buy_hold", "symbol": "BTCUSDT"},
        "buy_and_hold_eth": {"family": "buy_hold", "symbol": "ETHUSDT"},
        "equal_weight_universe_m30": {"family": "equal_weight", "rebalance": 30},
    }
    for name, params in benchmarks.items():
        full = run_one(panel, strategy, params, cost, 0, full_end, name)
        oos = run_one(panel, strategy, params, cost, oos_start, full_end, name)
        report["benchmarks"][name] = {
            "full": metrics(full),
            "full_yearly": periodic_returns(full, 4),
            "oos": metrics(oos),
            "oos_yearly": periodic_returns(oos, 4),
            "oos_equity": oos.equity,
            "oos_dates": oos.dates,
        }
        print(
            f"基准 {name}: 全样本年化={report['benchmarks'][name]['full']['cagr']:.1%} "
            f"夏普={report['benchmarks'][name]['full']['sharpe']:.2f} | "
            f"样本外年化={report['benchmarks'][name]['oos']['cagr']:.1%} "
            f"夏普={report['benchmarks'][name]['oos']['sharpe']:.2f}",
            flush=True,
        )

    for family in args.families.split(","):
        full_grid = GRIDS[family](True)
        wf_grid = full_grid
        is_rows = evaluate_grid(panel, strategy, full_grid, cost, 0, is_end)
        oos_rows = evaluate_grid(panel, strategy, full_grid, cost, oos_start, full_end)

        best_is = max(is_rows, key=lambda r: (r["sharpe"], r["max_drawdown"]))
        best_oos = max(oos_rows, key=lambda r: (r["sharpe"], r["max_drawdown"]))
        oos_sharpes = [r["sharpe"] for r in oos_rows]
        positive = sum(1 for s in oos_sharpes if s > 0) / len(oos_sharpes)
        dsr = deflated_sharpe(
            best_is["daily_returns"], [r["sharpe"] for r in is_rows], len(full_grid)
        )
        oos_of_best = next(
            r
            for r in oos_rows
            if json.dumps(r["params"], sort_keys=True)
            == json.dumps(best_is["params"], sort_keys=True)
        )
        wf_summary, wf_segments = walk_forward(panel, strategy, wf_grid, cost, wf_start)

        # 成本敏感性：用样本内选出的参数，在三种成本下看样本外表现。
        sensitivity = {}
        for label, (fee, slip) in {
            "零成本": (0.0, 0.0),
            "基准0.15%": (args.fee, args.slippage),
            "高成本0.30%": (0.002, 0.001),
        }.items():
            row = run_one(
                panel,
                strategy,
                best_is["params"],
                Cost(fee, slip),
                oos_start,
                full_end,
                label,
            )
            sensitivity[label] = metrics(row)

        verdict = {
            "walk_forward_sharpe_ge_0_8": wf_summary["sharpe"] >= 0.8,
            "walk_forward_return_positive": wf_summary["total_return"] > 0,
            "fixed_oos_return_positive": oos_of_best["total_return"] > 0,
            "grid_oos_positive_ratio_ge_60pct": positive >= 0.60,
            "walk_forward_drawdown_le_30pct": wf_summary["max_drawdown"] >= -0.30,
            "no_calendar_year_below_minus_30pct": all(
                v > -0.30 for v in wf_summary["yearly"].values()
            ),
            "bear_2022_full_year_validated": False,
        }
        verdict["passed"] = all(verdict.values())

        report["families"][family] = {
            "is_best": best_is,
            "oos_of_is_best": oos_of_best,
            "oos_best": best_oos,
            "grid_size": len(full_grid),
            "oos_sharpe_positive_ratio": positive,
            "oos_sharpe_distribution": {
                "min": min(oos_sharpes),
                "median": statistics.median(oos_sharpes),
                "max": max(oos_sharpes),
                "mean": statistics.fmean(oos_sharpes),
            },
            "deflated_sharpe": dsr,
            "walk_forward": wf_summary,
            "walk_forward_segments": wf_segments,
            "cost_sensitivity": sensitivity,
            "bootstrap": bootstrap_mean_ci(oos_of_best["daily_returns"]),
            "verdict": verdict,
            "oos_rows": [
                {
                    "params": r["params"],
                    "sharpe": r["sharpe"],
                    "cagr": r["cagr"],
                    "max_drawdown": r["max_drawdown"],
                    "turnover_per_year": r["turnover_per_year"],
                }
                for r in oos_rows
            ],
        }
        print(
            f"[{family}] IS 最优 {json.dumps(best_is['params'], ensure_ascii=False)} "
            f"IS 夏普={best_is['sharpe']:.2f} → OOS 夏普={oos_of_best['sharpe']:.2f}；"
            f"网格 OOS 正夏普比例={positive:.0%}；滚动前进 夏普={wf_summary['sharpe']:.2f} "
            f"回撤={wf_summary['max_drawdown']:.1%}；"
            f"判定={'通过' if verdict['passed'] else '未通过'}",
            flush=True,
        )

    (out_dir / "results.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(f"结果已写入 {out_dir / 'results.json'}", flush=True)


if __name__ == "__main__":
    main()
