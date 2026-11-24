"""协整检验与单位根检验（PRD FR-07.1，离线进化平面）。

本模块只做统计计算：不访问网络、不读配置、不写数据库，也不被交易 worker
的实时路径调用。它服务的是标的池协整性重评估 —— 用过去 60 天的小时级行情
判断某个价差配对的协整关系是否仍然成立。

口径
----
* 单位根检验：增广 Dickey-Fuller（ADF），回归式

  .. code-block:: text

      Δy_t = α + γ·y_{t-1} + Σ_{i=1..p} δ_i·Δy_{t-i} + ε_t

  原假设 H0 为"存在单位根"（序列非平稳），t 统计量显著为负才能拒绝 H0。
  价格序列与协整残差都用含常数项的 ``c``：**含趋势项的 ``ct`` 只适用于
  "确定性线性趋势"的数据生成过程**，用在随机游走的价格上会严重高估拒绝率
  （本模块的测试里，5% 水平上的误拒率会从 5% 抬到 31%），因此不作为默认口径。
  滞后阶数可以固定，也可以按 BIC 在 ``0..max_lag`` 中选择。
* 协整检验：Engle-Granger 两步法。先对对数价格做 OLS 得 ``y = a + β·x + e``，
  再对残差 ``e`` 做 ADF；H0 为"两序列不协整"。
* 半衰期：残差的 AR(1) 系数 ``φ``，半衰期为 ``-ln2/lnφ``；``φ`` 不落在 (0,1)
  时视为没有均值回复。

p 值
----
运行环境没有 statsmodels，p 值由 MacKinnon (1994) 的渐近临界值表插值得到，
是**近似值**：判定直接用 5% 临界值本身，决策不依赖 p 值的精确数值。
三张表分别是 :data:`ADF_CRITICAL_VALUES`、:data:`ADF_TREND_CRITICAL_VALUES`
与 :data:`ENGLE_GRANGER_CRITICAL_VALUES`，可以单独核对。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

#: ADF（含常数项，不含趋势）的渐近临界值：1% / 5% / 10%。
ADF_CRITICAL_VALUES = (-3.43, -2.86, -2.57)
#: ADF（含常数项与趋势）的渐近临界值：1% / 5% / 10%。
ADF_TREND_CRITICAL_VALUES = (-3.12, -2.64, -2.26)
#: Engle-Granger 两变量、无协整向量的渐近临界值：1% / 5% / 10%。
ENGLE_GRANGER_CRITICAL_VALUES = (-3.90, -3.34, -3.04)

#: z(0.95) 与 z(0.90)：p 值插值用的两个标准正态分位点。
_Z95 = 1.6448536269514722
_Z90 = 1.2815515655446004

#: 判定 ADF 时至少需要的回归样本数。
MIN_OBSERVATIONS = 30


@dataclass(frozen=True)
class CriticalValues:
    """渐近临界值表（1%、5%、10%）。"""

    one_percent: float
    five_percent: float
    ten_percent: float


@dataclass(frozen=True)
class AdfResult:
    """一次 ADF 检验的结果。"""

    #: ``γ`` 的 t 统计量；显著为负才拒绝单位根。
    statistic: float
    #: 使用的滞后阶数。
    lags: int
    #: 滞后阶数是按 BIC 选出来的，还是调用方指定的。
    lag_selected: bool
    #: 参与回归的样本数。
    observations: int
    #: 残差方差（回归尺度上的）。
    sigma_squared: float
    #: 使用的临界值表。
    critical_values: CriticalValues
    #: 由临界值表插值得到的 p 值近似值。
    p_value: float
    #: 是否在 5% 水平拒绝单位根（序列平稳）。
    stationary: bool


@dataclass(frozen=True)
class CointegrationResult:
    """一次 Engle-Granger 协整检验的结果。"""

    #: 对数价格 OLS 得到的协整系数 β（``y = a + β·x + e``）。
    hedge_ratio: float
    #: 对数价格 OLS 的截距。
    intercept: float
    #: 对残差做 ADF 得到的 t 统计量。
    adf_statistic: float
    #: 残差 ADF 使用的滞后阶数。
    adf_lags: int
    #: 残差 ADF 的回归样本数。
    observations: int
    #: 临界值表。
    critical_values: CriticalValues
    #: p 值近似值。
    p_value: float
    #: 是否在 5% 水平拒绝"不协整"（即判定为协整）。
    cointegrated: bool
    #: 残差 AR(1) 半衰期（单位与输入序列一致）；没有均值回复时为 ``None``。
    half_life: float | None
    #: 两条对数价格序列的相关系数。
    correlation: float


@dataclass(frozen=True)
class PairEvaluation:
    """一个配对的完整评估（FR-07.1 的判定单元）。"""

    symbol_a: str
    symbol_b: str
    #: 样本条数（对齐后的小时级观测数）。
    observations: int
    #: 覆盖的小时数。
    span_hours: float
    #: A 自身对数价格的 ADF 结果。
    adf_a: AdfResult
    #: B 自身对数价格的 ADF 结果。
    adf_b: AdfResult
    #: Engle-Granger 结果。
    cointegration: CointegrationResult
    #: 是否判定为协整（残差通过 ADF 且 Engle-Granger 在 5% 水平拒绝不协整）。
    cointegrated: bool
    #: 判定理由（中文，面向人工审阅）。
    reason: str
    #: 两腿是否都是 I(1)（自身 ADF 没有拒绝单位根）。
    both_integration_order_one: bool
    #: 前后半段各自的 EG 判定结果；``halves`` 为分段数。
    half_rejections: int
    halves: int
    #: 协整关系是否在前后半段都成立。
    stable: bool


class _Ols:
    """一次普通最小二乘拟合的内部结果。"""

    __slots__ = ("coefficients", "sigma_squared", "inverse")

    def __init__(self, coefficients: list[float], sigma_squared: float, inverse: list[list[float]]):
        self.coefficients = coefficients
        self.sigma_squared = sigma_squared
        self.inverse = inverse

    def t_statistic(self, index: int) -> float:
        variance = self.sigma_squared * self.inverse[index][index]
        if variance <= 0:
            return 0.0
        return self.coefficients[index] / math.sqrt(variance)


def _invert(matrix: list[list[float]]) -> list[list[float]]:
    """高斯-约当消元求逆（带部分主元选取）。"""
    size = len(matrix)
    augmented = [
        list(row) + [1.0 if i == j else 0.0 for j in range(size)] for i, row in enumerate(matrix)
    ]
    for col in range(size):
        pivot = max(range(col, size), key=lambda row: abs(augmented[row][col]))
        if abs(augmented[pivot][col]) < 1e-12:
            raise ValueError("设计矩阵奇异，无法回归")
        augmented[col], augmented[pivot] = augmented[pivot], augmented[col]
        divisor = augmented[col][col]
        augmented[col] = [value / divisor for value in augmented[col]]
        pivot_row = augmented[col]
        for row in range(size):
            if row == col:
                continue
            factor = augmented[row][col]
            if factor == 0.0:
                continue
            augmented[row] = [
                value - factor * element
                for value, element in zip(augmented[row], pivot_row, strict=True)
            ]
    return [row[size:] for row in augmented]


def _ols(design: list[list[float]], target: list[float]) -> _Ols:
    """最小二乘拟合，返回系数、残差方差与 ``(X'X)⁻¹``。"""
    rows = len(target)
    width = len(design[0])
    xtx = [[sum(row[i] * row[j] for row in design) for j in range(width)] for i in range(width)]
    xty = [sum(design[t][i] * target[t] for t in range(rows)) for i in range(width)]
    inverse = _invert(xtx)
    coefficients = [sum(inverse[i][j] * xty[j] for j in range(width)) for i in range(width)]
    residuals = [
        target[t] - sum(design[t][i] * coefficients[i] for i in range(width)) for t in range(rows)
    ]
    rss = sum(value * value for value in residuals)
    sigma_squared = rss / max(1, rows - width)
    return _Ols(coefficients, sigma_squared, inverse)


def _normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def approximate_p_value(statistic: float, critical_values: CriticalValues) -> float:
    """由临界值表插值得到 p 值近似值。

    做法是把 ``(5% 临界值, p=0.05)`` 与 ``(10% 临界值, p=0.10)`` 之间按标准正态
    分位点线性映射，再按正态尾概率外推：统计量越负，映射出的 z 越大、p 越小。
    三张表在 1% 临界值处的插值结果都落在 0.009～0.018 之间，量级可用。
    判定仍以临界值比较为准。
    """
    spread = critical_values.ten_percent - critical_values.five_percent
    if spread == 0:
        return 0.5
    factor = (_Z90 - _Z95) / spread
    z = _Z95 + (statistic - critical_values.five_percent) * factor
    return min(1.0, max(1e-6, 1.0 - _normal_cdf(z)))


def _adf_equation(
    values: list[float], lags: int, trend: bool
) -> tuple[list[list[float]], list[float]]:
    """构造 ADF 回归的设计矩阵与被解释变量。"""
    size = len(values)
    diffs = [0.0] * size
    for index in range(1, size):
        diffs[index] = values[index] - values[index - 1]
    design: list[list[float]] = []
    target: list[float] = []
    midpoint = size / 2
    for index in range(lags + 1, size):
        row = [1.0]
        if trend:
            row.append((index - midpoint) / size)
        row.append(values[index - 1])
        for offset in range(1, lags + 1):
            row.append(diffs[index - offset])
        design.append(row)
        target.append(diffs[index])
    return design, target


def _fit_adf(values: list[float], lags: int, trend: bool) -> _Ols:
    design, target = _adf_equation(values, lags, trend)
    return _ols(design, target)


def _bic_score(fit: _Ols, rows: int) -> float:
    width = len(fit.coefficients)
    return rows * math.log(fit.sigma_squared) + width * math.log(rows)


def _select_lag(values: list[float], max_lag: int, trend: bool) -> tuple[int, _Ols]:
    """按 BIC 选择 ADF 滞后阶数。"""
    best_lag = 0
    best_fit = _fit_adf(values, 0, trend)
    best_score = _bic_score(best_fit, len(values) - 1)
    for lag in range(1, max_lag + 1):
        if len(values) - lag - 1 < MIN_OBSERVATIONS:
            break
        fit = _fit_adf(values, lag, trend)
        score = _bic_score(fit, len(values) - lag - 1)
        if score < best_score:
            best_lag, best_fit, best_score = lag, fit, score
    return best_lag, best_fit


def adf_test(
    values: list[float],
    *,
    lag: int | None = None,
    max_lag: int = 12,
    trend: bool = False,
) -> AdfResult:
    """对一条序列做 ADF 检验。

    ``values`` 必须按时间升序排列，且不包含缺失值。``lag`` 为 ``None`` 时按 BIC
    在 ``0..max_lag`` 中选择滞后阶数；给定整数时固定使用该阶数（批量扫描时
    用来控制计算量）。
    """
    if lag is None:
        if len(values) < MIN_OBSERVATIONS + max_lag:
            raise ValueError(f"样本不足：{len(values)} 条，无法按 BIC 选择 ADF 滞后阶数")
        lags, fit = _select_lag(values, max_lag, trend)
        selected = True
    else:
        if lag < 0:
            raise ValueError("ADF 滞后阶数不能为负")
        if len(values) - lag - 1 < MIN_OBSERVATIONS:
            raise ValueError(f"样本不足：{len(values)} 条，无法做 ADF")
        lags = lag
        fit = _fit_adf(values, lags, trend)
        selected = False
    design, _ = _adf_equation(values, lags, trend)
    index = 2 if trend else 1
    statistic = fit.t_statistic(index)
    critical_values = CriticalValues(*(ADF_TREND_CRITICAL_VALUES if trend else ADF_CRITICAL_VALUES))
    p_value = approximate_p_value(statistic, critical_values)
    return AdfResult(
        statistic=statistic,
        lags=lags,
        lag_selected=selected,
        observations=len(design),
        sigma_squared=fit.sigma_squared,
        critical_values=critical_values,
        p_value=p_value,
        stationary=statistic < critical_values.five_percent,
    )


def _ar1_coefficient(values: list[float]) -> float | None:
    """序列的 AR(1) 系数（含常数项）。"""
    size = len(values)
    if size < 3:
        return None
    lagged = values[:-1]
    current = values[1:]
    mean_x = sum(lagged) / size
    mean_y = sum(current) / size
    sxx = sum((value - mean_x) ** 2 for value in lagged)
    if sxx <= 0:
        return None
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(lagged, current, strict=True))
    return sxy / sxx


def engle_granger_test(
    y_values: list[float],
    x_values: list[float],
    *,
    lag: int | None = None,
    max_lag: int = 12,
) -> CointegrationResult:
    """Engle-Granger 两步协整检验（输入为对数价格）。"""
    if len(y_values) != len(x_values):
        raise ValueError("两条序列长度不一致")
    size = len(y_values)
    if size < MIN_OBSERVATIONS:
        raise ValueError(f"样本不足：{size} 条，无法做协整检验")
    mean_x = sum(x_values) / size
    mean_y = sum(y_values) / size
    sxx = sum((value - mean_x) ** 2 for value in x_values)
    if sxx <= 0:
        raise ValueError("解释变量没有变异")
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(x_values, y_values, strict=True))
    syy = sum((value - mean_y) ** 2 for value in y_values)
    beta = sxy / sxx
    intercept = mean_y - beta * mean_x
    residuals = [y - intercept - beta * x for x, y in zip(x_values, y_values, strict=True)]
    syy_root = math.sqrt(syy)
    correlation = sxy / (math.sqrt(sxx) * syy_root) if syy_root > 0 else 0.0
    residual_adf = adf_test(residuals, lag=lag, max_lag=max_lag, trend=False)
    statistic = residual_adf.statistic
    critical_values = CriticalValues(*ENGLE_GRANGER_CRITICAL_VALUES)
    p_value = approximate_p_value(statistic, critical_values)
    phi = _ar1_coefficient(residuals)
    half_life = None
    if phi is not None and 0.0 < phi < 1.0:
        half_life = -math.log(2.0) / math.log(phi)
    return CointegrationResult(
        hedge_ratio=beta,
        intercept=intercept,
        adf_statistic=statistic,
        adf_lags=residual_adf.lags,
        observations=residual_adf.observations,
        critical_values=critical_values,
        p_value=p_value,
        cointegrated=statistic < critical_values.five_percent,
        half_life=half_life,
        correlation=correlation,
    )


def evaluate_pair(
    symbol_a: str,
    series_a: list[float],
    symbol_b: str,
    series_b: list[float],
    *,
    lag: int | None = None,
    max_lag: int = 12,
    span_hours: float | None = None,
    split_halves: bool = True,
) -> PairEvaluation:
    """评估一个配对是否仍然协整。

    ``series_a`` 与 ``series_b`` 是已经对齐的小时级**对数价格**。

    除了整段的 Engle-Granger，还做两件事：

    * 检查两腿是否都是 I(1) —— 有一条腿本身平稳时，"价差平稳"没有均值回复的
      含义，这类配对不是统计套利该做的标的；
    * 把样本对半切开各做一次 EG —— 只有前后半段都拒绝不协整，才认为协整关系
      仍然成立（PRD 说的"协整关系破裂"）。单段检验在趋势型标的上容易给出
      假协整，稳定性检验能挡掉一部分。
    """
    if len(series_a) != len(series_b):
        raise ValueError("两条序列长度不一致")
    adf_a = adf_test(series_a, lag=lag, max_lag=max_lag, trend=False)
    adf_b = adf_test(series_b, lag=lag, max_lag=max_lag, trend=False)
    cointegration = engle_granger_test(series_a, series_b, lag=lag, max_lag=max_lag)
    both_i1 = not adf_a.stationary and not adf_b.stationary
    halves = 0
    rejections = 0
    if split_halves and len(series_a) >= 2 * (MIN_OBSERVATIONS + (lag or 0)):
        middle = len(series_a) // 2
        for part_a, part_b in (
            (series_a[:middle], series_b[:middle]),
            (series_a[middle:], series_b[middle:]),
        ):
            halves += 1
            try:
                half = engle_granger_test(part_a, part_b, lag=lag, max_lag=max_lag)
            except ValueError:
                continue
            rejections += 1 if half.cointegrated else 0
    stable = cointegration.cointegrated and (halves == 0 or rejections == halves)
    if not cointegration.cointegrated:
        reason = "残差未通过 ADF，协整关系已破裂"
    elif not both_i1:
        reason = "有一条腿自身已平稳（I(0)），不是统计套利形态"
    elif not stable:
        reason = f"只有 {rejections}/{halves} 个半段通过协整检验，关系不稳"
    else:
        reason = "残差平稳、两腿均为 I(1)、前后半段都通过协整检验"
    return PairEvaluation(
        symbol_a=symbol_a,
        symbol_b=symbol_b,
        observations=len(series_a),
        span_hours=span_hours if span_hours is not None else float(len(series_a)),
        adf_a=adf_a,
        adf_b=adf_b,
        cointegration=cointegration,
        cointegrated=cointegration.cointegrated,
        reason=reason,
        both_integration_order_one=both_i1,
        half_rejections=rejections,
        halves=halves,
        stable=stable,
    )
