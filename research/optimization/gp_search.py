"""纯 Python 贝叶斯优化（PRD FR-07.2，离线进化平面）。

运行环境没有 Optuna / scipy / numpy，且部署镜像不应被研究依赖污染，所以这里
实现一个**可复现的高斯过程 + 期望改进（EI）**搜索器，专门用于 FR-07.2 的
三维参数空间（window、entry_z、exit_z）。

它不是通用优化库：

* 输入必须落在 ``[0, 1]^d``，由调用方负责与真实参数的线性映射；
* 核是平方指数核 + 观测噪声，超参数（长度尺度与噪声）固定并随结果一起报告；
* 采集函数 EI 的最大化用"确定性候选点 + 局部扰动"的多起点搜索，种子固定，
  同一份数据总能得到同一组结果；
* 目标函数可以是随机的（回测本身确定），但搜索过程记录每次试验。

**没有自动下发能力**：本模块只输出候选参数与评分，配置文件不会被改写。
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass

Vector = tuple[float, ...]


@dataclass(frozen=True)
class Trial:
    """一次目标函数评估。"""

    point: Vector
    value: float
    feasible: bool


@dataclass(frozen=True)
class BayesianResult:
    """搜索结果。"""

    best: Trial
    trials: list[Trial]
    length_scale: float
    noise: float
    seed: int


def _distance(a: Vector, b: Vector) -> float:
    return math.sqrt(
        sum((x - y) ** 2 for x, y in zip(a, b, strict=True))
    )


def _kernel(a: Vector, b: Vector, *, length_scale: float) -> float:
    distance = _distance(a, b)
    return math.exp(-0.5 * (distance / length_scale) ** 2)


def _cholesky(matrix: list[list[float]]) -> list[list[float]]:
    """Cholesky 分解；矩阵必须对称正定。"""
    size = len(matrix)
    lower = [[0.0] * size for _ in range(size)]
    for row in range(size):
        for col in range(row + 1):
            total = matrix[row][col] - sum(
                lower[row][k] * lower[col][k] for k in range(col)
            )
            if row == col:
                if total <= 1e-12:
                    total = 1e-12
                lower[row][col] = math.sqrt(total)
            else:
                lower[row][col] = total / lower[col][col]
    return lower


def _solve_lower(lower: list[list[float]], target: Sequence[float]) -> list[float]:
    """前代：解 L·y = b。"""
    result = [0.0] * len(target)
    for row in range(len(target)):
        total = sum(lower[row][k] * result[k] for k in range(row))
        result[row] = (target[row] - total) / lower[row][row]
    return result


def _solve_lower_transposed(lower: list[list[float]], target: Sequence[float]) -> list[float]:
    """回代：解 Lᵀ·x = b。"""
    size = len(target)
    result = [0.0] * size
    for row in range(size - 1, -1, -1):
        total = sum(lower[k][row] * result[k] for k in range(row + 1, size))
        result[row] = (target[row] - total) / lower[row][row]
    return result


def gp_predict(
    trials: Sequence[Trial], point: Vector, *, length_scale: float, noise: float
) -> tuple[float, float]:
    """高斯过程后验的均值与方差。"""
    values = [trial.value for trial in trials]
    matrix: list[list[float]] = [
        [
            _kernel(trial.point, other.point, length_scale=length_scale)
            + (noise if index == other_index else 0.0)
            for other_index, other in enumerate(trials)
        ]
        for index, trial in enumerate(trials)
    ]
    lower = _cholesky(matrix)
    alpha = _solve_lower_transposed(
        lower, _solve_lower(lower, values)
    )
    k_star = [
        _kernel(point, trial.point, length_scale=length_scale) for trial in trials
    ]
    mean = sum(k * a for k, a in zip(k_star, alpha, strict=True))
    v = _solve_lower(lower, k_star)
    variance = max(0.0, 1.0 - sum(value * value for value in v))
    return mean, math.sqrt(variance)


def expected_improvement(
    trials: Sequence[Trial], point: Vector, *, length_scale: float, noise: float
) -> float:
    """EI 采集函数（越大越值得试）。"""
    if not trials:
        return 1.0
    best = max(trial.value for trial in trials if trial.feasible)
    mean, stdev = gp_predict(trials, point, length_scale=length_scale, noise=noise)
    if stdev <= 1e-12:
        return 0.0
    z = (mean - best) / stdev
    return (mean - best) * 0.5 * (1.0 + math.erf(z / math.sqrt(2.0))) + stdev * math.exp(
        -0.5 * z * z
    ) / math.sqrt(2.0 * math.pi)


def _clamp(point: Vector) -> Vector:
    return tuple(min(1.0, max(0.0, value)) for value in point)


def optimize(
    objective: Callable[[Vector], float],
    *,
    dimensions: int,
    n_trials: int = 60,
    n_initial: int = 12,
    length_scale: float = 0.35,
    noise: float = 0.05,
    seed: int = 20261123,
    candidate_pool: int = 256,
) -> BayesianResult:
    """在 ``[0,1]^dimensions`` 上最大化目标函数。

    ``objective`` 返回 ``float('-inf')`` 表示这组参数不可用（例如闭环太少）。
    前 ``n_initial`` 个点是固定种子的 Sobol 风格低差异点（加抖动），之后按 EI
    选下一个点；整个过程可复现。
    """
    if dimensions < 1 or n_trials < n_initial or n_initial < 2:
        raise ValueError("参数空间、试验数与初始点数不合法")
    rng = random.Random(seed)  # noqa: S311 - 确定性科研搜索，不是加密用途
    trials: list[Trial] = []
    for index in range(n_initial):
        # 低差异基底 + 小抖动，兼顾覆盖与可复现。
        point = tuple(
            min(
                1.0,
                max(
                    0.0,
                    ((index + 1) * (k + 1) / (dimensions + 1)) % 1.0
                    + rng.uniform(-0.05, 0.05),
                ),
            )
            for k in range(dimensions)
        )
        value = objective(point)
        trials.append(Trial(point=point, value=value, feasible=math.isfinite(value)))

    for _ in range(n_trials - n_initial):
        candidates: list[Vector] = []
        for _ in range(candidate_pool):
            base = rng.choice(trials).point
            if rng.random() < 0.4:
                candidate = _clamp(tuple(rng.random() for _ in range(dimensions)))
            else:
                candidate = _clamp(
                    tuple(value + rng.gauss(0, 0.12) for value in base)
                )
            candidates.append(candidate)
        scored = [
            (expected_improvement(trials, point, length_scale=length_scale, noise=noise), point)
            for point in candidates
        ]
        scored.sort(reverse=True)
        _, point = scored[0]
        if any(_distance(point, trial.point) < 1e-6 for trial in trials):
            point = _clamp(tuple(value + 1e-4 for value in point))
        value = objective(point)
        trials.append(Trial(point=point, value=value, feasible=math.isfinite(value)))
    feasible = [trial for trial in trials if trial.feasible]
    if not feasible:
        raise ValueError("所有试验都不可行，无法选出最优参数")
    return BayesianResult(
        best=max(feasible, key=lambda trial: trial.value),
        trials=trials,
        length_scale=length_scale,
        noise=noise,
        seed=seed,
    )
