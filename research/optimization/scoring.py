"""参数评分用的滚动回测（PRD FR-07.2，离线进化平面）。

本模块只做纯计算：输入对齐好的小时级价格与一组参数，输出这组参数的样本内
表现（净盈亏、夏普、最大回撤、闭环笔数）。

口径完全照搬影子盘（`spread/shadow.py`）：

* 对数价差 ``ln(A) - β·ln(B)``；
* 用总体标准差的滑动 Z-Score，判定顺序 成本过滤 → 止损 → 开仓 → 平仓；
* 一笔闭环的净盈亏 = 价差收敛盈亏 − 双边手续费 − 双边滑点（按名义金额）。

**没有真实成交滑点分布可用**（NAS 当前 0 笔成交、影子表里只有 1 条非零
模拟滑点），所以成本只能用已知配置参数的保守估计。报告里必须显式标注。

这个模块不属于交易服务镜像；被优化器调用，产出的参数只做人工审阅。
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Parameters:
    """FR-07.2 的三个寻优维度（Z 止损沿用窗口值的固定倍数）。"""

    window: int
    entry_z: float
    exit_z: float
    stop_z: float

    def __post_init__(self) -> None:
        if self.window < 5 or self.window > 500:
            raise ValueError("window 超出 5..500")
        if not (0 <= self.exit_z < self.entry_z < self.stop_z):
            raise ValueError("必须满足 0 <= exit_z < entry_z < stop_z")
        if not all(math.isfinite(value) for value in (self.entry_z, self.exit_z, self.stop_z)):
            raise ValueError("阈值必须是有限数")


@dataclass(frozen=True)
class CostModel:
    """已知参数下的双边交易摩擦估计。"""

    fee_rate: float = 0.00055
    slippage_bps: float = 5.0
    funding_buffer: float = 0.0

    def __post_init__(self) -> None:
        if any(value < 0 for value in (self.fee_rate, self.slippage_bps, self.funding_buffer)):
            raise ValueError("成本参数不能为负")

    def per_notional(self, notional: float) -> float:
        """一次开仓（双边）的摩擦成本。"""
        fee = notional * self.fee_rate * 2
        slippage = notional * self.slippage_bps / 10000 * 2
        return fee + slippage + self.funding_buffer


@dataclass(frozen=True)
class BacktestResult:
    """参数评分结果。"""

    parameters: Parameters
    observations: int
    closed_trades: int
    mean_pnl: float
    stdev_pnl: float
    net_pnl: float
    sharpe: float
    max_drawdown: float
    cost_total: float
    #: 可用样本不足（例如窗口没填满）时，结果只供查看、不参与寻优排名。
    sufficient: bool


def rolling_zscores(values: list[float], window: int) -> list[float | None]:
    """滑动窗口 Z-Score；窗口未填满前返回 ``None``。

    标准差用总体方差（除以 N），与 ``spread/signals.py`` 的 ``rolling_stats`` 一致。
    """
    if window < 2:
        raise ValueError("window 至少为 2")
    output: list[float | None] = []
    for index in range(len(values)):
        if index + 1 < window:
            output.append(None)
            continue
        tail = values[index + 1 - window : index + 1]
        mean = sum(tail) / window
        variance = sum((value - mean) ** 2 for value in tail) / window
        stdev = math.sqrt(variance)
        output.append((values[index] - mean) / stdev if stdev > 0 else None)
    return output


def backtest_parameters(
    log_spreads: list[float],
    parameters: Parameters,
    *,
    notional: float,
    cost_model: CostModel,
    min_closed_trades: int = 3,
    burn_in_bars: int = 0,
    score_start: int | None = None,
) -> BacktestResult:
    """按“上一根产生信号、下一根收盘成交”跑一遍保守回测。

    ``log_spreads`` 可以包含验证窗之前的历史，用于计算滑动统计量；
    ``score_start`` 指定从第几根开始实际计入交易，之前的预热段只用于
    形成 Z-Score，不会产生信号或交易。这样验证窗的窗口长度不会受限于
    7 天数据，同时仍然不会用未来数据。
    """
    if notional <= 0:
        raise ValueError("notional 必须为正")
    if burn_in_bars < 0 or not 0 <= burn_in_bars <= len(log_spreads):
        raise ValueError("burn_in_bars 超出序列范围")
    if score_start is None:
        score_start = burn_in_bars
    if not 0 <= score_start <= len(log_spreads):
        raise ValueError("score_start 超出序列范围")
    zscores = rolling_zscores(log_spreads, parameters.window)
    trade_start = max(burn_in_bars, score_start, parameters.window)
    position: int | None = None  # +1 = 做空 A 做多 B（spread 高），-1 反之
    entry_z = 0.0
    entry_spread = 0.0
    pnls: list[float] = []
    costs: list[float] = []
    equity = 0.0
    open_cost = 0.0
    peak = 0.0
    max_dd = 0.0
    for index in range(trade_start, len(log_spreads) - 1):
        z = zscores[index]
        if z is None:
            continue
        # index 这根收盘后产生信号；index + 1 这根收盘成交。
        fill_index = index + 1
        if position is None:
            if abs(z) >= parameters.entry_z:
                cost = cost_model.per_notional(notional)
                if abs(z) * notional <= cost:
                    # 成本过滤：预期收敛空间不够，不开仓。
                    continue
                position = 1 if z > 0 else -1
                open_cost = cost
                entry_z = z
                entry_spread = log_spreads[fill_index]
                costs.append(cost)
            continue
        exit_trade = abs(z) <= parameters.exit_z
        stop_trade = abs(z) >= parameters.stop_z or z * entry_z < 0
        if not (exit_trade or stop_trade):
            continue
        pnl = (
            (entry_spread - log_spreads[fill_index]) * position * notional - open_cost
        )
        equity += pnl
        peak = max(peak, equity)
        if peak > 0:
            max_dd = max(max_dd, (peak - equity) / peak)
        pnls.append(pnl)
        position = None
    closed = len(pnls)
    mean = sum(pnls) / closed if closed else 0.0
    stdev = math.sqrt(sum((value - mean) ** 2 for value in pnls) / closed) if closed > 1 else 0.0
    sharpe = mean / stdev * math.sqrt(24 * 252) if stdev > 0 else 0.0
    observations = sum(
        1 for index in range(trade_start, len(zscores)) if zscores[index] is not None
    )
    return BacktestResult(
        parameters=parameters,
        observations=observations,
        closed_trades=closed,
        mean_pnl=mean,
        stdev_pnl=stdev,
        net_pnl=equity,
        sharpe=sharpe,
        max_drawdown=max_dd,
        cost_total=sum(costs),
        sufficient=closed >= min_closed_trades and observations >= parameters.window,
    )
