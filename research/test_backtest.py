#!/usr/bin/env python3
"""回测引擎自检：用可手算的小样本验证记账、成交与成本。

运行：``python research/test_backtest.py``
"""

from __future__ import annotations

import math
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from backtest import (  # noqa: E402
    Cost,
    Panel,
    StrategySet,
    join_results,
    metrics,
    periodic_returns,
    run_backtest,
)

DAY = 86_400_000


def make_panel(prices: list[list[float]], symbols: list[str]) -> Panel:
    """构造一个只有收盘/开盘、全部有数据的极简面板。"""
    n = len(prices[0])
    dates = [DAY * (i + 1) for i in range(n)]
    close = [row[:] for row in prices]
    open_ = [row[:] for row in prices]
    has = [[True] * n for _ in symbols]
    vol = [[0.5] * n for _ in symbols]
    eligible = [list(range(len(symbols))) for _ in range(n)]
    return Panel(dates, symbols, close, open_, has, vol, eligible)


def test_buy_and_hold_matches_price_change() -> None:
    # 开盘价等于当日价格：100 -> 110 -> 121。
    panel = make_panel([[100.0, 110.0, 121.0]], ["AAA"])
    result = run_backtest(
        panel, lambda i, nav: {"AAA": 1.0} if i == 0 else None, Cost(0, 0), 0, 2, "bh"
    )
    assert abs(result.equity[-1] - 1.21) < 1e-9, result.equity
    assert result.trades == 2, result.trades  # 建仓 + 末端平仓


def test_cost_is_charged_on_entry_and_exit() -> None:
    # 记账约定：目标权重是**成本后**净值的份额，手续费按成交名义额收取，
    # 因此满仓建仓买到的资产只值 nav/(1+rate)，卖出时再扣一次。
    panel = make_panel([[100.0, 110.0, 121.0]], ["AAA"])
    rate = 0.001
    result = run_backtest(
        panel, lambda i, nav: {"AAA": 1.0} if i == 0 else None, Cost(rate, 0), 0, 2, "bh"
    )
    expected = 1.21 * (1 - rate) / (1 + rate)
    assert abs(result.equity[-1] - expected) < 1e-12, (result.equity[-1], expected)


def test_target_is_fraction_of_post_cost_nav() -> None:
    # 满仓买入不能透支：1 单位净值在 1% 成本后只剩 0.99，两端各扣一次。
    panel = make_panel([[100.0, 100.0, 100.0]], ["AAA"])
    result = run_backtest(
        panel, lambda i, nav: {"AAA": 1.0} if i == 0 else None, Cost(0.01, 0), 0, 2, "x"
    )
    assert abs(result.equity[-1] - (1 - 0.01) / (1 + 0.01)) < 1e-12, result.equity


def test_no_signal_means_hold_quantities() -> None:
    panel = make_panel([[100.0, 110.0, 121.0]], ["AAA"])
    result = run_backtest(panel, lambda i, nav: None, Cost(0, 0), 0, 2, "hold")
    assert result.equity == [1.0, 1.0, 1.0], result.equity
    assert result.trades == 0


def test_empty_target_liquidates() -> None:
    panel = make_panel([[100.0, 110.0, 121.0]], ["AAA"])
    calls = {"n": 0}

    def weights(i: int, nav: float):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"AAA": 1.0}
        if calls["n"] == 2:
            return {}
        return None

    result = run_backtest(panel, weights, Cost(0, 0), 0, 2, "flat")
    # 第 1 天开盘买入（100），第 2 天开盘卖出（110），净值停在 1.10。
    assert abs(result.equity[-1] - 1.10) < 1e-9, result.equity


def test_rebalance_keeps_weights_constant() -> None:
    panel = make_panel([[100.0, 100.0, 100.0], [100.0, 100.0, 100.0]], ["AAA", "BBB"])
    result = run_backtest(panel, lambda i, nav: {"AAA": 0.5, "BBB": 0.5}, Cost(0, 0), 0, 2, "ew")
    assert abs(result.equity[-1] - 1.0) < 1e-9, result.equity


def test_join_results_compounds_without_overlap() -> None:
    panel = make_panel([[100.0, 110.0, 121.0, 133.1, 146.4]], ["AAA"])
    first = run_backtest(panel, lambda i, nav: None, Cost(0, 0), 0, 2, "a")
    second = run_backtest(panel, lambda i, nav: None, Cost(0, 0), 2, 4, "b")
    joined = join_results([first, second], "joined")
    assert len(joined.daily_returns) == len(first.daily_returns) + len(second.daily_returns)
    assert abs(joined.equity[-1] - 1.0) < 1e-12
    assert len(set(joined.dates)) == len(joined.dates)


def test_metrics_on_flat_series() -> None:
    panel = make_panel([[100.0, 100.0, 100.0]], ["AAA"])
    result = run_backtest(panel, lambda i, nav: None, Cost(0, 0), 0, 2, "flat")
    m = metrics(result)
    assert m["sharpe"] == 0.0 and m["max_drawdown"] == 0.0 and m["vol"] == 0.0


def test_missing_price_blocks_rebalance() -> None:
    panel = make_panel([[100.0, 110.0, 121.0]], ["AAA"])
    panel.has[0][0] = False  # 建仓当天没有行情
    result = run_backtest(
        panel, lambda i, nav: {"AAA": 1.0} if i == 0 else None, Cost(0, 0), 0, 2, "x"
    )
    assert result.meta["skipped_rebalances"] == 1, result.meta
    assert result.trades == 0


def test_missing_boundary_quote_cannot_create_cash() -> None:
    panel = make_panel([[100.0, 110.0, 121.0]], ["AAA"])
    panel.has[0][-1] = False
    try:
        run_backtest(
            panel,
            lambda i, nav: {"AAA": 1.0} if i == 0 else None,
            Cost(0, 0),
            0,
            2,
            "missing-exit",
        )
    except ValueError as exc:
        assert "boundary quotes" in str(exc)
    else:
        raise AssertionError("A historical quote must not fabricate an executable exit")


def test_hourly_annualization_and_join_frequency() -> None:
    panel = make_panel([[100.0, 101.0, 100.0, 102.0, 100.0]], ["AAA"])
    panel.periods_per_year = 8760
    panel.dates = [3_600_000 * i for i in range(5)]
    first = run_backtest(panel, lambda i, nav: {"AAA": 1.0}, Cost(0, 0), 0, 2, "first")
    second = run_backtest(panel, lambda i, nav: {"AAA": 1.0}, Cost(0, 0), 2, 4, "second")
    joined = join_results([first, second], "hourly")
    hourly = metrics(joined)
    assert joined.periods_per_year == 8760
    assert math.isclose(hourly["years"], 4 / 8760)
    assert len(set(joined.dates)) == 4
    joined.periods_per_year = 365
    daily = metrics(joined)
    assert math.isclose(hourly["sharpe"], daily["sharpe"] * math.sqrt(24))
    assert math.isclose(hourly["turnover_per_year"], daily["turnover_per_year"] * 24)


def test_rejected_entry_restores_strategy_state() -> None:
    panel = make_panel([[100.0, 110.0, 121.0, 133.1]], ["BTCUSDT"])
    panel.has[0][0] = False
    strategy = StrategySet(panel)
    result = run_backtest(
        panel,
        strategy.make("buy_hold", {"symbol": "BTCUSDT"}),
        Cost(0, 0),
        1,
        3,
        "retry",
    )
    assert math.isclose(result.equity[-1], 133.1 / 121.0)

    panel = make_panel([[100.0, 100.0, 100.0, 100.0]], ["BTCUSDT"])
    weights = StrategySet(panel).make("buy_hold", {"symbol": "BTCUSDT"})
    assert weights(1, 1.0) == {"BTCUSDT": 1.0}
    weights.on_execution(False)
    assert weights(2, 1.0) == {"BTCUSDT": 1.0}


def test_year_boundary_returns_use_interval_start() -> None:
    from backtest import Backtest

    result = Backtest("years", [0.1, -0.1], [1.0, 1.1, 0.99], ["2025-01-01", "2025-01-02"])
    yearly = periodic_returns(result)
    assert math.isclose(yearly["2024"], 0.1)
    assert math.isclose(yearly["2025"], -0.1)


def test_walk_forward_covers_tail_and_lags_selection() -> None:
    import run_research as study

    panel = make_panel([[100.0] * 10], ["BTCUSDT"])
    old_train, old_step = study.WF_TRAIN, study.WF_STEP
    try:
        study.WF_TRAIN, study.WF_STEP = 3, 3
        summary, segments = study.walk_forward(
            panel,
            StrategySet(panel),
            [{"family": "buy_hold", "symbol": "BTCUSDT"}],
            Cost(0, 0),
            4,
        )
    finally:
        study.WF_TRAIN, study.WF_STEP = old_train, old_step
    assert summary["periods"] == 5
    assert segments[-1]["apply_end"] == "1970-01-11"
    assert segments[-1]["periods"] == 2
    assert len(segments) == 2
    assert all(s["train_end"] < s["apply_start"] for s in segments)
    assert summary["execution"]["terminal_liquidation"]


def test_chart_keeps_dates_aligned_and_normalizes() -> None:
    from make_chart import build_series

    dates = ["2024-01-02", "2024-01-03"]
    curve = {"dates": dates, "equity": [1.0, 2.0, 4.0]}
    results = {
        "benchmarks": {
            name: {"oos_dates": dates, "oos_equity": [1.0, 2.0, 4.0]}
            for name in ("buy_and_hold_btc", "equal_weight_universe_m30")
        },
        "families": {name: {"walk_forward": curve} for name in ("tsmom", "cross", "reversion")},
    }
    for _, actual_dates, equity in build_series(results, "2024-01-01"):
        assert actual_dates == ["2024-01-01", *dates]
        assert equity == [1.0, 2.0, 4.0]
    for _, actual_dates, equity in build_series(results, "2024-01-02"):
        assert actual_dates == dates
        assert equity == [1.0, 2.0]


def main() -> None:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"{len(tests)} 项自检通过")


if __name__ == "__main__":
    main()
