#!/usr/bin/env python3
"""FR-07.2 滚动参数寻优命令行（离线只读，不自动下发）。

复用 ``check_pairs.py`` 采集并缓存的小时线（``hourly-closes.json``），对指定
配对做 30 天训练 / 7 天验证的滚动寻优，输出候选参数与验证期表现。

.. code-block:: bash

    python research/optimization/optimize.py \
        --data var/cointegration/2026-11-23-top50/hourly-closes.json \
        --pair BTCUSDT:ETHUSDT --hedge-ratio 0.7143 \
        --out var/optimization/2026-11-23-btc-eth
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from walk_forward import atomic_json, run_walk_forward  # noqa: E402

DISCLAIMER = (
    "FR-07.2 候选参数仅用于人工审阅：没有真实成交滑点分布（当前 0 笔成交），"
    "成本用配置假设；不自动改配置、不灰度、不实盘晋级。"
)


def _render(report: dict[str, object]) -> str:
    summary = report["summary"]
    assert isinstance(summary, dict)
    windows = report["windows"]
    assert isinstance(windows, list)
    lines = [
        "# FR-07.2 滚动参数寻优报告",
        "",
        f"- 配对：{report['pair']}，β = {report['hedge_ratio']}，名义 {report['notional']} USDT",
        "- 成本假设：单边费率 {fee}，单边滑点 {slip} bps（{source}）".format(
            fee=report["cost_model"]["fee_rate"],
            slip=report["cost_model"]["slippage_bps"],
            source=report["cost_model"]["source"],
        ),
        (
            "- 滚动窗口：{n} 个；寻优参数优于默认参数的窗口：{w}/{n}；"
            "验证期有闭环的窗口：{t}/{n}"
        ).format(
            n=len(windows),
            w=summary["better_than_default"],
            t=summary["windows_with_valid_trades"],
        ),
        "- 验证期平均夏普：默认 {a:.2f} → 寻优 {b:.2f}".format(
            a=summary["default_sharpe_mean"], b=summary["optimized_sharpe_mean"]
        ),
        "",
        "## 各窗口明细",
        "",
        (
            "| 验证窗（UTC） | window | entry_z | exit_z | stop_z | "
            "验证净盈亏 | 验证夏普 | 默认夏普 | 验证闭环数 |"
        ),
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for item in windows:
        assert isinstance(item, dict)
        best = item["best"]
        valid = item["valid"]
        default = item["default"]
        lines.append(
            "| {window} | {w} | {e:.3f} | {x:.3f} | {s:.3f} | {pnl:.2f} | {sharpe:.2f} | "
            "{default_sharpe:.2f} | {trades} |".format(
                window=f"{item['valid_window'][0][:10]}~{item['valid_window'][1][:10]}",
                w=best["window"],
                e=best["entry_z"],
                x=best["exit_z"],
                s=best["stop_z"],
                pnl=valid["net_pnl"],
                sharpe=valid["sharpe"],
                default_sharpe=default["sharpe"],
                trades=valid["closed_trades"],
            )
        )
    lines += [
        "",
        f"- {report['disclaimer']}",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="FR-07.2 滚动参数寻优（离线只读）")
    parser.add_argument("--data", type=pathlib.Path, required=True, help="hourly-closes.json 路径")
    parser.add_argument("--pair", required=True, help="形如 BTCUSDT:ETHUSDT")
    parser.add_argument("--hedge-ratio", type=float, required=True)
    parser.add_argument("--notional", type=float, default=100.0)
    parser.add_argument("--fee-rate", type=float, default=0.00055)
    parser.add_argument("--slippage-bps", type=float, default=5.0)
    parser.add_argument("--train-days", type=int, default=30)
    parser.add_argument("--valid-days", type=int, default=7)
    parser.add_argument("--step-days", type=int, default=7)
    parser.add_argument("--n-trials", type=int, default=50)
    parser.add_argument("--n-initial", type=int, default=10)
    parser.add_argument("--seed", type=int, default=20261123)
    parser.add_argument("--out", type=pathlib.Path, default=pathlib.Path("var/optimization"))
    args = parser.parse_args()
    if ":" not in args.pair:
        parser.error("--pair 必须形如 BTCUSDT:ETHUSDT")
    symbol_a, symbol_b = args.pair.split(":", 1)
    if args.hedge_ratio <= 0:
        parser.error("--hedge-ratio 必须为正")
    if args.n_trials < args.n_initial or args.n_initial < 2:
        parser.error("--n-trials 必须大于 --n-initial（至少 2）")

    raw = json.loads(args.data.read_text(encoding="utf-8"))
    closes = {
        symbol: {int(stamp): float(close) for stamp, close in series.items()}
        for symbol, series in raw.get("closes", {}).items()
    }
    report = run_walk_forward(
        closes,
        symbol_a,
        symbol_b,
        hedge_ratio=args.hedge_ratio,
        notional=args.notional,
        fee_rate=args.fee_rate,
        slippage_bps=args.slippage_bps,
        train_days=args.train_days,
        valid_days=args.valid_days,
        step_days=args.step_days,
        n_trials=args.n_trials,
        n_initial=args.n_initial,
        seed=args.seed,
    )
    report["disclaimer"] = DISCLAIMER
    atomic_json(args.out / "optimization.json", report)
    (args.out / "optimization.md").write_text(_render(report), encoding="utf-8")
    print(
        json.dumps(
            {
                "out": str(args.out),
                "pair": report["pair"],
                "windows": report["summary"]["windows"],
                "better_than_default": report["summary"]["better_than_default"],
                "default_sharpe_mean": report["summary"]["default_sharpe_mean"],
                "optimized_sharpe_mean": report["summary"]["optimized_sharpe_mean"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 -- CLI 边界统一成一行错误
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
