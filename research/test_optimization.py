#!/usr/bin/env python3
"""FR-07.2 自检：回测口径、贝叶斯优化、滚动窗口。

纯计算与合成数据，不需要网络与数据库：

.. code-block:: bash

    python research/test_optimization.py
"""

from __future__ import annotations

import math
import pathlib
import random
import sys

_ROOT = pathlib.Path(__file__).resolve().parent / "optimization"
sys.path.insert(0, str(_ROOT))

from gp_search import Trial, expected_improvement, gp_predict, optimize  # noqa: E402
from scoring import CostModel, Parameters, backtest_parameters, rolling_zscores  # noqa: E402
from walk_forward import _decode, _windows, run_walk_forward  # noqa: E402


def _series(seed: int, length: int = 2000) -> tuple[list[float], dict[str, dict[int, float]]]:
    """造一条协整的合成对数价格序列。"""
    rng = random.Random(seed)
    x, error = 10.0, 0.0
    values: list[float] = []
    for _ in range(length):
        x += 0.0002 + rng.gauss(0, 0.01)
        error = 0.7 * error + rng.gauss(0, 0.01)
        values.append(1.5 * x + error)
    return values, {}


def test_rolling_zscores_waits_for_window() -> None:
    """窗口未填满前不能产生信号，避免用未来数据。"""
    values = [float(index) for index in range(100)]
    z = rolling_zscores(values, 20)
    assert all(value is None for value in z[:19])
    assert z[19] is not None
    assert len(z) == len(values)


def test_parameters_reject_invalid_thresholds() -> None:
    """阈值必须满足 exit < entry < stop。"""
    for kwargs in (
        {"window": 60, "entry_z": 1.0, "exit_z": 2.0, "stop_z": 3.0},
        {"window": 60, "entry_z": 2.0, "exit_z": 1.0, "stop_z": 1.5},
        {"window": 4, "entry_z": 2.0, "exit_z": 0.5, "stop_z": 3.5},
    ):
        try:
            Parameters(**kwargs)
        except ValueError:
            continue
        raise AssertionError(f"非法参数被接受：{kwargs}")


def test_cost_model_matches_reported_both_sides() -> None:
    """成本模型按双边收费，与信号层的成本过滤公式一致。"""
    model = CostModel(fee_rate=0.00055, slippage_bps=5.0)
    expected = 100 * 0.00055 * 2 + 100 * 5.0 / 10000 * 2
    assert abs(model.per_notional(100) - expected) < 1e-12
    assert abs(expected - 0.21) < 1e-9


def test_backtest_pnl_includes_cost_once() -> None:
    """每笔闭环只扣一次成本，净盈亏等于各笔 PnL 之和。"""
    values, _ = _series(2, length=1500)
    result = backtest_parameters(
        values,
        Parameters(window=60, entry_z=2.0, exit_z=0.5, stop_z=3.5),
        notional=100,
        cost_model=CostModel(),
    )
    assert result.closed_trades > 0
    assert abs(result.net_pnl - result.mean_pnl * result.closed_trades) < 1e-9
    assert abs(result.cost_total - result.closed_trades * CostModel().per_notional(100)) < 1e-9


def test_backtest_is_deterministic() -> None:
    """同一份数据与参数必须得到完全一样的评分。"""
    values, _ = _series(3, length=800)
    params = Parameters(window=40, entry_z=1.8, exit_z=0.6, stop_z=3.2)
    first = backtest_parameters(values, params, notional=100, cost_model=CostModel())
    second = backtest_parameters(values, params, notional=100, cost_model=CostModel())
    assert first == second


def test_decode_maps_unit_cube_to_valid_parameters() -> None:
    """单位立方体映射到合法参数域；违反 exit<entry 的点直接判为不可用。"""
    params = _decode((0.1, 0.5, 0.2))
    assert params is not None
    assert 5 <= params.window <= 500
    assert 1.0 <= params.entry_z <= 4.0
    assert 0.1 <= params.exit_z <= 2.0
    assert params.exit_z < params.entry_z < params.stop_z
    assert _decode((0.5, 0.0, 0.99)) is None


def test_gp_predict_and_ei_are_finite() -> None:
    """GP 后验与 EI 在少量观测下也要给出有限值。"""
    trials = [
        Trial(point=(0.1, 0.2, 0.3), value=0.5, feasible=True),
        Trial(point=(0.8, 0.7, 0.6), value=1.2, feasible=True),
    ]
    mean, stdev = gp_predict(trials, (0.5, 0.5, 0.5), length_scale=0.35, noise=0.05)
    assert math.isfinite(mean) and math.isfinite(stdev) and stdev >= 0
    ei = expected_improvement(
        trials, (0.5, 0.5, 0.5), length_scale=0.35, noise=0.05
    )
    assert math.isfinite(ei)


def test_bayesian_optimizer_recovers_quadratic_maximum() -> None:
    """贝叶斯优化必须能在可复现条件下找到简单二次目标的最优附近。"""
    target = (0.7, 0.3, 0.6)
    objective = lambda point: -sum((a - b) ** 2 for a, b in zip(point, target, strict=True))  # noqa: E731
    first = optimize(objective, dimensions=3, n_trials=40, n_initial=10, seed=11)
    second = optimize(objective, dimensions=3, n_trials=40, n_initial=10, seed=11)
    assert first.best == second.best
    assert first.best.value > -0.02, first.best


def test_validation_window_uses_prior_history_for_warmup() -> None:
    """验证窗可以短于滑动窗口：用验证前历史预热，但只在验证段计分。"""
    values, _ = _series(21, length=1000)
    params = Parameters(window=300, entry_z=1.5, exit_z=0.5, stop_z=3.0)
    no_history = backtest_parameters(
        values[700:],
        params,
        notional=100,
        cost_model=CostModel(),
    )
    with_history = backtest_parameters(
        values,
        params,
        notional=100,
        cost_model=CostModel(),
        burn_in_bars=700,
        score_start=700,
    )
    assert no_history.observations == 0
    assert no_history.closed_trades == 0
    assert with_history.observations > 0
    assert with_history.closed_trades > 0


def test_signal_fills_on_next_bar() -> None:
    """信号在 index 产生，成交价必须取 index+1 的收盘价。"""
    values = [0.0] * 10 + [5.0] * 10
    params = Parameters(window=5, entry_z=1.0, exit_z=0.1, stop_z=3.0)
    result = backtest_parameters(
        values,
        params,
        notional=100,
        cost_model=CostModel(fee_rate=0, slippage_bps=0),
    )
    # 第 10 根达到入场条件，成交应在第 11 根；本例没有后续出场信号，
    # 所以不应产生闭环，也不应把入场信号当作同一根成交的收益。
    assert result.closed_trades == 0
    assert result.net_pnl == 0.0


def test_walk_forward_windows_cover_data_without_leakage() -> None:
    """验证窗必须在训练窗之后，且两者不重叠。"""
    windows = _windows(100, train_days=2, valid_days=1, step_days=1)
    assert windows == [(0, 48, 48, 72), (24, 72, 72, 96)]


def test_walk_forward_reports_cost_and_disclaimer() -> None:
    """滚动报告必须标明成本来源与"不自动下发"的边界。"""
    rng = random.Random(9)
    closes: dict[str, dict[int, float]] = {"AAAUSDT": {}, "BBBUSDT": {}}
    stamp = 1_700_000_000_000
    x, error = 10.0, 0.0
    for _ in range(45 * 24):
        x += 0.0002 + rng.gauss(0, 0.01)
        error = 0.7 * error + rng.gauss(0, 0.01)
        closes["AAAUSDT"][stamp] = math.exp(x)
        closes["BBBUSDT"][stamp] = math.exp((x - error) / 1.5)
        stamp += 3_600_000
    report = run_walk_forward(
        closes,
        "AAAUSDT",
        "BBBUSDT",
        hedge_ratio=1.5,
        notional=100,
        train_days=30,
        valid_days=7,
        step_days=7,
        n_trials=8,
        n_initial=4,
    )
    assert report["windows"]
    assert "real slippage distribution unavailable" in report["cost_model"]["source"]
    assert "不自动" in report["disclaimer"]


def main() -> None:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"{len(tests)} 项自检通过")


if __name__ == "__main__":
    main()
