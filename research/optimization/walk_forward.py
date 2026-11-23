"""滚动窗口参数寻优管线（PRD FR-07.2，离线进化平面）。

输入是 FR-07.1 已经缓存下来的小时线（只有收盘价），按 PRD 的"过去 30 天"
口径切成 30 天训练 / 7 天验证的滚动窗口，对每个窗口跑贝叶斯优化，得出
``window / entry_z / exit_z``，并用 ``stop_z = entry_z × 1.75`` 的派生值
（与当前配置的 2.0/3.5 比例一致）交给评分器。

**数据局限（必须写进报告）**：

* PRD 要的是"过去 30 天真实成交滑点分布"，NAS 当前 0 笔成交、影子表里只有
  1 条非零模拟滑点，不足以构成分布。因此成本只能用已知配置参数（单边
  0.055%、单边 5bps）做保守估计，不能声称是真实滑点。
* 输入是小时**收盘价**，没有盘口深度、没有真实成交时点，回测是候选参数
  的粗略筛选，不能替代 FR-07.3 的 7 天影子验证。

本模块只读缓存数据，只输出报告；不写配置、不写数据库、不启用任何执行。
"""

from __future__ import annotations

import json
import math
import pathlib
import sys
from dataclasses import dataclass
from typing import Any

_LOCAL = pathlib.Path(__file__).resolve().parent
if str(_LOCAL) not in sys.path:
    sys.path.insert(0, str(_LOCAL))

# 研究脚本既支持 python 文件直接运行，也支持 pytest 收集；路径显式注入避免
# 与 research/backtest.py 的同名模块相撞。
from gp_search import BayesianResult, optimize  # noqa: E402
from scoring import (  # noqa: E402
    BacktestResult,
    CostModel,
    Parameters,
    backtest_parameters,
)

DAY_MS = 86_400_000
HOUR_MS = 3_600_000

#: 参数搜索范围（与配置模型的合法域对齐）。
WINDOW_RANGE = (5, 500)
ENTRY_RANGE = (1.0, 4.0)
EXIT_RANGE = (0.1, 2.0)
#: stop_z 按 entry_z 的比例派生，避免优化器生成不满足 exit<entry<stop 的三元组。
STOP_MULTIPLIER = 1.75

DEFAULT_TRAIN_DAYS = 30
DEFAULT_VALID_DAYS = 7
DEFAULT_STEP_DAYS = 7


@dataclass(frozen=True)
class WindowResult:
    """一个滚动窗口的寻优结果。"""

    train_start: str
    train_end: str
    valid_start: str
    valid_end: str
    best: Parameters
    train_score: BacktestResult
    valid_score: BacktestResult
    default_score: BacktestResult
    n_trials: int
    seed: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "train_window": [self.train_start, self.train_end],
            "valid_window": [self.valid_start, self.valid_end],
            "best": {
                "window": self.best.window,
                "entry_z": self.best.entry_z,
                "exit_z": self.best.exit_z,
                "stop_z": self.best.stop_z,
            },
            "train": {
                "net_pnl": self.train_score.net_pnl,
                "sharpe": self.train_score.sharpe,
                "max_drawdown": self.train_score.max_drawdown,
                "closed_trades": self.train_score.closed_trades,
            },
            "valid": {
                "net_pnl": self.valid_score.net_pnl,
                "sharpe": self.valid_score.sharpe,
                "max_drawdown": self.valid_score.max_drawdown,
                "closed_trades": self.valid_score.closed_trades,
            },
            "default": {
                "net_pnl": self.default_score.net_pnl,
                "sharpe": self.default_score.sharpe,
                "max_drawdown": self.default_score.max_drawdown,
                "closed_trades": self.default_score.closed_trades,
            },
            "n_trials": self.n_trials,
            "seed": self.seed,
        }


def _decode(point: tuple[float, ...]) -> Parameters | None:
    """把 ``[0,1]^3`` 映射到参数域。"""
    if len(point) != 3:
        raise ValueError("参数维度不对")
    window = round(WINDOW_RANGE[0] + point[0] * (WINDOW_RANGE[1] - WINDOW_RANGE[0]))
    entry = ENTRY_RANGE[0] + point[1] * (ENTRY_RANGE[1] - ENTRY_RANGE[0])
    exit_z = EXIT_RANGE[0] + point[2] * (EXIT_RANGE[1] - EXIT_RANGE[0])
    if not (0 <= exit_z < entry):
        return None
    stop = entry * STOP_MULTIPLIER
    try:
        return Parameters(
            window=window,
            entry_z=round(entry, 3),
            exit_z=round(exit_z, 3),
            stop_z=round(stop, 3),
        )
    except ValueError:
        return None


def _iso(ms: int) -> str:
    import datetime as dt

    return dt.datetime.fromtimestamp(ms / 1000, dt.UTC).isoformat()


def _score(
    log_spreads: list[float],
    params: Parameters,
    *,
    notional: float,
    cost_model: CostModel,
    score_start: int = 0,
    burn_in_bars: int = 0,
) -> BacktestResult:
    return backtest_parameters(
        log_spreads,
        params,
        notional=notional,
        cost_model=cost_model,
        burn_in_bars=burn_in_bars,
        score_start=score_start,
    )


def _objective(
    point: tuple[float, ...],
    *,
    train: list[float],
    notional: float,
    cost_model: CostModel,
) -> float:
    params = _decode(point)
    if params is None:
        return float("-inf")
    result = _score(train, params, notional=notional, cost_model=cost_model)
    if not result.sufficient:
        return float("-inf")
    # 目标 = 年化夏普与净收益的混合；用闭环数做轻微加权，避免只有一笔大赚的过拟合。
    # 闭环太少时夏普极易失真（3 笔里 2 笔小赚就能算出几百的年化值），所以按闭环数缩放。
    confidence = min(1.0, result.closed_trades / 20.0)
    return (result.sharpe + result.net_pnl / notional * 0.01) * confidence


def _windows(
    total_bars: int, *, train_days: int, valid_days: int, step_days: int
) -> list[tuple[int, int, int, int]]:
    """返回若干个 ``(train_start, train_end, valid_start, valid_end)`` 下标。"""
    train_bars = train_days * 24
    valid_bars = valid_days * 24
    step_bars = step_days * 24
    if train_bars + valid_bars > total_bars:
        return []
    windows: list[tuple[int, int, int, int]] = []
    start = 0
    while start + train_bars + valid_bars <= total_bars:
        windows.append(
            (start, start + train_bars, start + train_bars, start + train_bars + valid_bars)
        )
        start += step_bars
    return windows


def run_walk_forward(
    closes: dict[str, dict[int, float]],
    symbol_a: str,
    symbol_b: str,
    *,
    hedge_ratio: float,
    notional: float,
    fee_rate: float = 0.00055,
    slippage_bps: float = 5.0,
    train_days: int = DEFAULT_TRAIN_DAYS,
    valid_days: int = DEFAULT_VALID_DAYS,
    step_days: int = DEFAULT_STEP_DAYS,
    n_trials: int = 50,
    n_initial: int = 10,
    seed: int = 20261123,
    default_parameters: Parameters | None = None,
) -> dict[str, Any]:
    """对一个配对做滚动寻优。"""
    if symbol_a not in closes or symbol_b not in closes:
        raise ValueError("缺少配对的小时线")
    if notional <= 0:
        raise ValueError("notional 必须为正")
    common = sorted(set(closes[symbol_a]) & set(closes[symbol_b]))
    if len(common) < (train_days + valid_days) * 24:
        raise ValueError(f"样本不足：{len(common)} 根，无法做 {train_days}+{valid_days} 天滚动寻优")
    log_spreads = [
        math.log(closes[symbol_a][stamp]) - hedge_ratio * math.log(closes[symbol_b][stamp])
        for stamp in common
    ]
    cost_model = CostModel(fee_rate=fee_rate, slippage_bps=slippage_bps)
    default = default_parameters or Parameters(window=60, entry_z=2.0, exit_z=0.5, stop_z=3.5)
    windows = _windows(
        len(common), train_days=train_days, valid_days=valid_days, step_days=step_days
    )
    results: list[WindowResult] = []
    for index, (train_start, train_end, valid_start, valid_end) in enumerate(windows):
        train = log_spreads[train_start:train_end]
        # 验证期用训练段与更早的数据预热滑动统计量，但 score_start 之前不产生交易。
        valid_with_history = log_spreads[:valid_end]

        def objective(point: tuple[float, ...], *, _train: list[float] = train) -> float:
            return _objective(
                point, train=_train, notional=notional, cost_model=cost_model
            )

        bayes: BayesianResult = optimize(
            objective,
            dimensions=3,
            n_trials=n_trials,
            n_initial=n_initial,
            seed=seed + index,
        )
        best = _decode(bayes.best.point)
        if best is None:
            continue
        results.append(
            WindowResult(
                train_start=_iso(common[train_start]),
                train_end=_iso(common[train_end - 1]),
                valid_start=_iso(common[valid_start]),
                valid_end=_iso(common[valid_end - 1]),
                best=best,
                train_score=_score(train, best, notional=notional, cost_model=cost_model),
                valid_score=_score(
                    valid_with_history,
                    best,
                    notional=notional,
                    cost_model=cost_model,
                    burn_in_bars=valid_start,
                    score_start=valid_start,
                ),
                default_score=_score(
                    valid_with_history,
                    default,
                    notional=notional,
                    cost_model=cost_model,
                    burn_in_bars=valid_start,
                    score_start=valid_start,
                ),
                n_trials=len(bayes.trials),
                seed=seed + index,
            )
        )
    if not results:
        raise ValueError("没有可用的滚动窗口结果")
    wins = sum(1 for item in results if item.valid_score.sharpe > item.default_score.sharpe)
    traded = sum(1 for item in results if item.valid_score.closed_trades > 0)
    return {
        "pair": f"{symbol_a}:{symbol_b}",
        "hedge_ratio": hedge_ratio,
        "notional": notional,
        "cost_model": {
            "fee_rate": fee_rate,
            "slippage_bps": slippage_bps,
            "source": (
                "configured assumption; real slippage distribution unavailable (0 executions)"
            ),
        },
        "windows": [item.as_dict() for item in results],
        "summary": {
            "windows": len(results),
            "better_than_default": wins,
            "windows_with_valid_trades": traded,
            "default_sharpe_mean": sum(
                item.default_score.sharpe for item in results
            ) / len(results),
            "optimized_sharpe_mean": sum(
                item.valid_score.sharpe for item in results
            ) / len(results),
            "recommendation": "待人工审阅；不构成自动下发或灰度依据",
        },
        "disclaimer": (
            "本结果基于小时收盘价与配置假设成本，不是真实成交滑点分布；"
            "不能替代 FR-07.3 的 7 天影子验证，也不自动改配置或启用执行。"
        ),
    }


def atomic_json(path: pathlib.Path, value: object) -> None:
    """原子写 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    import os
    import tempfile

    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        pathlib.Path(temporary).replace(path)
    except BaseException:
        pathlib.Path(temporary).unlink(missing_ok=True)
        raise
