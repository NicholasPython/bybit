#!/usr/bin/env python3
"""把回测结果整理成 Markdown 表格（结论叙述写在 research/report.md）。"""

from __future__ import annotations

import argparse
import json
import pathlib


def fmt_pct(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:.1%}"


def fmt_num(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "—"
    return f"{value:.{digits}f}"


def main() -> None:
    parser = argparse.ArgumentParser(description="生成研究结果表格")
    parser.add_argument("--results", default="research/results/results.json")
    parser.add_argument("--options", default="research/results/options_results.json")
    args = parser.parse_args()

    results = json.loads(pathlib.Path(args.results).read_text(encoding="utf-8"))

    print("## 基准（全样本）\n")
    print("| 策略 | 年化 | 波动 | 夏普 | 最大回撤 | 换手/年 |")
    print("| --- | --- | --- | --- | --- | --- |")
    for name, row in results["benchmarks"].items():
        m = row["full"]
        print(
            f"| {name} | {fmt_pct(m['cagr'])} | {fmt_pct(m['vol'])} | {fmt_num(m['sharpe'])} "
            f"| {fmt_pct(m['max_drawdown'])} | {fmt_num(m['turnover_per_year'])} |"
        )

    for family, block in results.get("families", {}).items():
        print(f"\n## 策略族：{family}\n")
        best_is = block["is_best"]
        oos = block["oos_of_is_best"]
        wf = block["walk_forward"]
        dist = block["oos_sharpe_distribution"]
        print(
            f"- 样本内最优参数：`{json.dumps(best_is['params'], ensure_ascii=False)}`"
            f"（IS 夏普 {fmt_num(best_is['sharpe'])}）"
        )
        print(
            f"- 该参数在样本外：夏普 {fmt_num(oos['sharpe'])}、年化 {fmt_pct(oos['cagr'])}、"
            f"最大回撤 {fmt_pct(oos['max_drawdown'])}"
        )
        print(
            f"- 参数网格（{block['grid_size']} 组）在样本外的夏普分布："
            f"最小 {fmt_num(dist['min'])}、中位 {fmt_num(dist['median'])}、"
            f"最大 {fmt_num(dist['max'])}；正夏普比例 {fmt_pct(block['oos_sharpe_positive_ratio'])}"
        )
        print(
            f"- 滚动前进（{wf['segments']} 段）：夏普 {fmt_num(wf['sharpe'])}、"
            f"年化 {fmt_pct(wf['cagr'])}、总收益 {fmt_pct(wf['total_return'])}、"
            f"最大回撤 {fmt_pct(wf['max_drawdown'])}"
        )
        if wf.get("yearly"):
            yearly = "、".join(f"{y}: {fmt_pct(v)}" for y, v in wf["yearly"].items())
            print(f"- 滚动前进分年：{yearly}")
        verdict = block.get("verdict", {})
        flags = "、".join(
            f"{k}={'通过' if v else '未通过'}" for k, v in verdict.items() if k != "passed"
        )
        print(f"- 判定：{flags} → **{'通过' if verdict.get('passed') else '未通过'}**")

    options_path = pathlib.Path(args.options)
    if options_path.exists():
        options = json.loads(options_path.read_text(encoding="utf-8"))
        print("\n## 期权策略\n")
        print("| 标的 | 策略 | 收益 | 夏普 | 最大回撤 | 手续费/权利金 |")
        print("| --- | --- | --- | --- | --- | --- |")
        for base, block in options.get("options", {}).items():
            if "error" in block:
                print(f"| {base} | — | — | — | — | {block['error']} |")
                continue
            for row in block["results"]:
                if "sharpe" not in row:
                    continue
                print(
                    f"| {base} | {row['name']} | {fmt_pct(row['total_return'])} "
                    f"| {fmt_num(row['sharpe'])} | {fmt_pct(row['max_drawdown'])} "
                    f"| {fmt_pct(row['avg_fee_share'])} |"
                )
            window = block.get("window")
            if window:
                print(f"\n{base} 样本窗口：{window[0]} ~ {window[1]}（{window[2]} 天）\n")


if __name__ == "__main__":
    main()
