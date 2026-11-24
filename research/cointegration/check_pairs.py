#!/usr/bin/env python3
"""标的池协整性重评估（PRD FR-07.1，离线进化平面）。

按 PRD 要求：取流动性前 N 个 U 本位永续合约过去 D 天的小时级行情，跑 ADF
与 Engle-Granger 协整检验，把"在册配对是否还协整"和"新合格配对"写成报告。

**它只产出报告**：不写数据库、不改配置、不启用任何交易执行，也不自动晋级。
剔除与提名都要人工审阅后再决定。

运行：

.. code-block:: bash

    python research/cointegration/check_pairs.py --out var/cointegration/latest \\
        --pair BTCUSDT:ETHUSDT
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from pool import (  # noqa: E402
    DAY_MS,
    DEFAULT_DAYS,
    DEFAULT_MIN_OBSERVATIONS,
    DEFAULT_TOP_SYMBOLS,
    HOUR_MS,
    PairResult,
    PublicDataError,
    atomic_json,
    build_universe,
    evaluate_pool,
    fetch_public_many,
    kline_jobs,
    load_hourly_closes,
    negative_beta,
)

DISCLAIMER = (
    "本报告只用于人工审阅：不会自动改配置、不会启用执行、不会灰度或实盘晋级。"
    "p 值为 MacKinnon 渐近临界值表插值得到的近似值，判定以 5% 临界值比较为准。"
)


def _now_hour_ms() -> int:
    """当前整点（毫秒）；K 线窗口不含未收盘的那一根。"""
    now_ms = int(time.time() * 1000)
    return now_ms - now_ms % HOUR_MS


def _render_markdown(report: dict[str, object]) -> str:
    meta = report["meta"]
    assert isinstance(meta, dict)
    lines: list[str] = []
    lines.append("# 标的池协整性重评估报告")
    lines.append("")
    lines.append(f"- 生成时间（UTC）：{meta['generated_utc']}")
    lines.append(
        f"- 数据窗口：{meta['window_start_utc']} 起 {meta['days']} 天，"
        f"小时级收盘价，样本 {meta['expected_bars']} 根/合约"
    )
    lines.append(f"- 池子：流动性前 {meta['universe_size']} 个 U 本位永续合约")
    lag_text = "按 BIC 选滞后阶数" if meta["adf_lag"] is None else f"固定滞后 {meta['adf_lag']} 阶"
    lines.append(f"- ADF 口径：{lag_text}，含常数项，5% 水平")
    lines.append(f"- 取数通道：{meta['transport']}")
    source_text = "复用本地缓存" if meta.get("data_source") == "reused" else "本次采集"
    lines.append(f"- 行情来源：{source_text}")
    lines.append("")
    lines.append("## 在册配对")
    lines.append("")
    lines.append("| 配对 | 结论 | β | ADF t | p（近似） | 半衰期(h) | 相关性 | 样本 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for item in report["in_book"]:
        assert isinstance(item, dict)
        verdict = "保持" if item["classification"] == "keep" else "剔除"
        half_life = item["half_life_hours"]
        lines.append(
            (
                "| {pair} | {verdict} | {beta:.4f} | {stat:.3f} | {p:.4f} "
                "| {hl} | {corr:.3f} | {obs} |"
            ).format(
                pair=item["pair"],
                verdict=verdict,
                beta=item["hedge_ratio"],
                stat=item["adf_statistic"],
                p=item["p_value"],
                hl="—" if half_life is None else f"{half_life:.1f}",
                corr=item["correlation"],
                obs=item["observations"],
            )
        )
    lines.append("")
    lines.append("## 新提名配对（待人工审阅）")
    lines.append("")
    if report["nominations"]:
        lines.append("| 配对 | β | ADF t | p（近似） | 半衰期(h) | 相关性 | 样本 |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        for item in report["nominations"]:
            assert isinstance(item, dict)
            half_life = item["half_life_hours"]
            lines.append(
                "| {pair} | {beta:.4f} | {stat:.3f} | {p:.4f} | {hl} | {corr:.3f} | {obs} |".format(
                    pair=item["pair"],
                    beta=item["hedge_ratio"],
                    stat=item["adf_statistic"],
                    p=item["p_value"],
                    hl="—" if half_life is None else f"{half_life:.1f}",
                    corr=item["correlation"],
                    obs=item["observations"],
                )
            )
    else:
        lines.append("本次没有通过检验的新配对。")
    lines.append("")
    others = report["other_cointegrated_pairs"]
    lines.append(f"另有 {others} 个配对通过检验但未提名（按 p 值与半衰期排序截断）。")
    lines.append("")
    inverse = report.get("inverse_beta_pairs") or []
    if inverse:
        lines.append("## 反向关系（β ≤ 0，当前配置模型不接受）")
        lines.append("")
        lines.append("| 配对 | β | ADF t | p（近似） | 半衰期(h) |")
        lines.append("| --- | --- | --- | --- | --- |")
        for item in inverse:
            half_life = item["half_life_hours"]
            lines.append(
                "| {pair} | {beta:.4f} | {stat:.3f} | {p:.4f} | {hl} |".format(
                    pair=item["pair"],
                    beta=item["hedge_ratio"],
                    stat=item["adf_statistic"],
                    p=item["p_value"],
                    hl="—" if half_life is None else f"{half_life:.1f}",
                )
            )
    lines.append("")
    lines.append("## 说明")
    lines.append("")
    lines.append(f"- {DISCLAIMER}")
    skipped_count = len(report["skipped_pairs"])
    dropped_count = len(report["dropped_symbols"])
    lines.append(f"- 样本不足而跳过的配对：{skipped_count} 个；被剔除的合约：{dropped_count} 个。")
    lines.append("")
    return "\n".join(lines)


def _split_results(
    results: list[PairResult],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    in_book: list[dict[str, object]] = []
    nominations: list[dict[str, object]] = []
    for item in results:
        payload = item.as_dict()
        if item.classification in {"keep", "drop"}:
            in_book.append(payload)
        elif item.classification == "nominate":
            nominations.append(payload)
    return in_book, nominations


def run(args: argparse.Namespace) -> dict[str, object]:
    end_ms = _now_hour_ms()
    start_ms = end_ms - args.days * DAY_MS
    fetch = lambda jobs: fetch_public_many(jobs, ssh_host=args.ssh_host)  # noqa: E731

    quotes, universe_skipped = build_universe(fetch, top=args.top)
    if not quotes:
        raise PublicDataError("没有取到任何可用的永续合约")
    symbols = [quote.symbol for quote in quotes]
    cache_path = args.out / "hourly-closes.json"
    closes: dict[str, dict[int, float]] = {}
    dropped: list[str] = []
    data_source = "fetched"
    if args.reuse_data and cache_path.exists():
        raw = json.loads(cache_path.read_text(encoding="utf-8"))
        closes = {symbol: {int(stamp): float(close) for stamp, close in series.items()}
                  for symbol, series in raw.get("closes", {}).items()}
        dropped = list(raw.get("dropped", []))
        data_source = "reused"
    if not closes:
        jobs = kline_jobs(symbols, start_ms, end_ms)
        envelope = fetch(jobs)
        closes, dropped = load_hourly_closes(envelope, symbols, start_ms=start_ms, end_ms=end_ms)
        atomic_json(
            cache_path,
            {
                "window_start_ms": start_ms,
                "window_end_ms": end_ms,
                "dropped": dropped,
                "closes": {symbol: {str(stamp): close for stamp, close in series.items()}
                           for symbol, series in closes.items()},
            },
        )
    usable = [quote for quote in quotes if quote.symbol in closes]
    if len(usable) < 2:
        raise PublicDataError("可用合约不足两个，无法做配对检验")

    results, skipped_pairs, others = evaluate_pool(
        usable,
        closes,
        configured_pairs=args.pair,
        min_observations=args.min_observations,
        adf_lag=args.adf_lag,
        max_nominees=args.max_nominees,
    )
    in_book, nominations = _split_results(results)
    inverse = [item.as_dict() for item in negative_beta(results)]

    report: dict[str, object] = {
        "meta": {
            "generated_utc": dt.datetime.now(dt.UTC).isoformat(),
            "days": args.days,
            "interval": "60",
            "window_start_utc": dt.datetime.fromtimestamp(
                start_ms / 1000, dt.UTC
            ).isoformat(),
            "window_end_utc": dt.datetime.fromtimestamp(end_ms / 1000, dt.UTC).isoformat(),
            "expected_bars": (end_ms - start_ms) // HOUR_MS,
            "universe_size": len(usable),
            "adf_lag": args.adf_lag,
            "adf_lag_mode": "auto" if args.adf_lag is None else "fixed",
            "min_observations": args.min_observations,
            "configured_pairs": list(args.pair),
            "transport": (
                f"local ssh -> {args.ssh_host} stdlib https GET; public only; no credentials"
            ),
            "data_source": data_source,
            "disclaimer": DISCLAIMER,
        },
        "universe": [
            {
                "symbol": quote.symbol,
                "turnover_24h": quote.turnover_24h,
                "contract_type": quote.contract_type,
                "status": quote.status,
            }
            for quote in usable
        ],
        "in_book": in_book,
        "nominations": nominations,
        "inverse_beta_pairs": inverse,
        "other_cointegrated_pairs": others,
        "skipped_pairs": skipped_pairs,
        "skipped_symbols": universe_skipped,
        "dropped_symbols": dropped,
    }
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description="标的池协整性重评估（FR-07.1，离线只读）"
    )
    parser.add_argument("--out", type=pathlib.Path, default=pathlib.Path("var/cointegration"))
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--top", type=int, default=DEFAULT_TOP_SYMBOLS)
    parser.add_argument("--ssh-host", default="nas")
    parser.add_argument(
        "--pair",
        action="append",
        default=[],
        help="当前在册的配对（可重复），形如 BTCUSDT:ETHUSDT",
    )
    parser.add_argument(
        "--adf-lag",
        default="2",
        help="ADF 固定滞后阶数；填 auto 表示按 BIC 选择",
    )
    parser.add_argument("--min-observations", type=int, default=DEFAULT_MIN_OBSERVATIONS)
    parser.add_argument("--max-nominees", type=int, default=10)
    parser.add_argument(
        "--reuse-data",
        action="store_true",
        help="复用输出目录里已采集的小时线（hourly-closes.json），不重新取数",
    )
    args = parser.parse_args()
    if args.days < 1:
        parser.error("--days 必须为正")
    if args.top < 2:
        parser.error("--top 至少为 2")
    if args.min_observations < 60:
        parser.error("--min-observations 至少为 60")
    if args.adf_lag.lower() == "auto":
        args.adf_lag = None
    else:
        try:
            args.adf_lag = int(args.adf_lag)
        except ValueError:
            parser.error("--adf-lag 必须是整数或 auto")
        if args.adf_lag < 0:
            parser.error("--adf-lag 不能为负")

    report = run(args)
    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    atomic_json(out / "cointegration.json", report)
    (out / "cointegration.md").write_text(_render_markdown(report), encoding="utf-8")
    summary = {
        "out": str(out),
        "universe": report["meta"]["universe_size"],
        "in_book": [
            {
                "pair": item["pair"],
                "classification": item["classification"],
                "p_value": item["p_value"],
                "half_life_hours": item["half_life_hours"],
            }
            for item in report["in_book"]
        ],
        "nominations": [item["pair"] for item in report["nominations"]],
        "other_cointegrated_pairs": report["other_cointegrated_pairs"],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 -- CLI 边界：统一成一行错误
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
