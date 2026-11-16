#!/usr/bin/env python3
"""生成同一日期与同一起点归一的净值图，以及可检查的逐日数值表。"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import json
import pathlib

PALETTE = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4"]
SURFACE = "#fcfcfb"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
W, H = 960, 540
PAD_L, PAD_R, PAD_T, PAD_B = 64, 170, 66, 80


def build_series(results: dict, start_day: str) -> list[tuple[str, list[str], list[float]]]:
    candidates = []
    for name, label in (
        ("buy_and_hold_btc", "BTC 买入持有"),
        ("equal_weight_universe_m30", "全市场等权"),
    ):
        block = results["benchmarks"][name]
        candidates.append((label, block["oos_dates"], block["oos_equity"]))
    for family, label in (
        ("tsmom", "时序动量"),
        ("cross", "横截面动量"),
        ("reversion", "均值回归"),
    ):
        block = results["families"][family]["walk_forward"]
        candidates.append((label, block["dates"], block["equity"]))
    series = []
    for label, dates, equity in candidates:
        # Each date is a return's ending boundary; equity[0] precedes dates[0].
        if len(equity) != len(dates) + 1:
            raise ValueError(f"Equity/date mismatch: {label}")
        begin = (dt.date.fromisoformat(dates[0]) - dt.timedelta(days=1)).isoformat()
        boundaries = [begin, *dates]
        points = {
            date: value for date, value in zip(boundaries, equity, strict=True) if date >= start_day
        }
        series.append((label, points))
    common = sorted(set.intersection(*(set(points) for _, points in series)))
    if len(common) < 2:
        raise ValueError("Insufficient common dates")
    return [
        (label, common, [points[date] / points[common[0]] for date in common])
        for label, points in series
    ]


def render_svg(series: list, start_day: str) -> str:
    values = [v for _, _, equity in series for v in equity]
    lo, hi = min(values), max(values)
    span = hi - lo or 1.0
    lo, hi = lo - span * 0.06, hi + span * 0.06
    span = hi - lo
    dates = series[0][1]
    first, last = (dt.date.fromisoformat(d) for d in (dates[0], dates[-1]))

    def x_of(day: str) -> float:
        elapsed = (dt.date.fromisoformat(day) - first).days / (last - first).days
        return PAD_L + (W - PAD_L - PAD_R) * elapsed

    def y_of(value: float) -> float:
        return H - PAD_B - (H - PAD_T - PAD_B) * (value - lo) / span

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
        f'viewBox="0 0 {W} {H}" role="img" aria-labelledby="title desc" '
        'font-family="system-ui, -apple-system, sans-serif">',
        '<title id="title">Bybit 样本外净值比较</title>',
        '<desc id="desc">五条曲线在共同日期归一，含手续费与滑点。逐日值另存 CSV。</desc>',
        f'<rect width="{W}" height="{H}" fill="{SURFACE}"/>',
        f'<text x="{PAD_L}" y="26" font-size="17" font-weight="600" fill="{INK}">'
        f"样本外净值比较（{html.escape(start_day)} 起）</text>",
        f'<text x="{PAD_L}" y="48" font-size="12" fill="{MUTED}">'
        "共同起点归一为 1；策略为连续滚动选参组合；含手续费与滑点</text>",
    ]
    for step in range(6):
        value = lo + span * step / 5
        y = y_of(value)
        parts += [
            f'<line x1="{PAD_L}" y1="{y:.1f}" x2="{W - PAD_R}" y2="{y:.1f}" '
            f'stroke="{GRID}" stroke-width="1"/>',
            f'<text x="{PAD_L - 10}" y="{y + 4:.1f}" font-size="11" fill="{MUTED}" '
            f'text-anchor="end">{value:.1f}</text>',
        ]
    for day in (dates[0], "2025-01-01", "2026-01-01", dates[-1]):
        if dates[0] <= day <= dates[-1]:
            x = x_of(day)
            parts.append(
                f'<text x="{x:.1f}" y="{H - PAD_B + 23}" font-size="11" '
                f'fill="{MUTED}" text-anchor="middle">{day}</text>'
            )
    parts.append(
        f'<line x1="{PAD_L}" y1="{H - PAD_B}" x2="{W - PAD_R}" '
        f'y2="{H - PAD_B}" stroke="{AXIS}" stroke-width="1"/>'
    )
    for index, (label, days, equity) in enumerate(series):
        points = " ".join(
            f"{x_of(day):.1f},{y_of(value):.1f}" for day, value in zip(days, equity, strict=True)
        )
        parts.append(
            f'<polyline points="{points}" fill="none" stroke="{PALETTE[index]}" '
            'stroke-width="2" stroke-linejoin="round" stroke-linecap="round">'
            f"<title>{html.escape(label)}：末值 {equity[-1]:.3f}</title></polyline>"
        )
    # Extreme endpoint labels stay apart; all series have a legend and table values.
    finals = sorted(range(len(series)), key=lambda i: series[i][2][-1])
    for index in (finals[0], finals[-1]):
        label, _, equity = series[index]
        parts.append(
            f'<text x="{W - PAD_R + 10}" y="{y_of(equity[-1]) + 4:.1f}" '
            f'font-size="12" fill="{INK_2}">{html.escape(label)} {equity[-1]:.2f}</text>'
        )
    for index, (label, _, _) in enumerate(series):
        x = PAD_L + index * 168
        parts += [
            f'<rect x="{x}" y="{H - 30}" width="14" height="3" fill="{PALETTE[index]}"/>',
            f'<text x="{x + 20}" y="{H - 22}" font-size="11" '
            f'fill="{INK_2}">{html.escape(label)}</text>',
        ]
    parts.append("</svg>")
    return "\n".join(parts)


def main() -> None:
    parser = argparse.ArgumentParser(description="生成归一净值曲线图与数值表")
    parser.add_argument("--results", default="research/results/results.json")
    parser.add_argument("--out", default="research/results/equity_curves.svg")
    parser.add_argument("--start", default="2024-01-01")
    args = parser.parse_args()
    results = json.loads(pathlib.Path(args.results).read_text(encoding="utf-8"))
    series = build_series(results, args.start)
    out = pathlib.Path(args.out)
    out.write_text(render_svg(series, args.start), encoding="utf-8")
    with out.with_suffix(".csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["date", *[label for label, _, _ in series]])
        for i, date in enumerate(series[0][1]):
            writer.writerow([date, *[equity[i] for _, _, equity in series]])
    print(f"已写入 {out} 与逐日 CSV")


if __name__ == "__main__":
    main()
