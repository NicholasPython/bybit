"""金额、数量与精度处理。

需求约束：
    * 金额和数量使用 Decimal（MKT-01 / §5.1）；
    * 精度向**不扩大支出**的方向处理（SPT-02）；
    * 交易规则随合约变化，步长以合约规格为准（MKT-01）。
"""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_HALF_UP, Decimal
from enum import StrEnum


class Rounding(StrEnum):
    """舍入方向。"""

    #: 向下取整 —— 用于数量：宁可少买，不扩大支出。
    DOWN = "down"
    #: 向上取整 —— 用于价格上限：保证限价不超过预算。
    UP = "up"
    #: 四舍五入 —— 用于报告与展示。
    HALF_UP = "half_up"
    #: 贴最近 —— 用于价格（限价落在刻度上）。
    NEAREST = "nearest"


_ZERO = Decimal("0")


def dec(value: str | int | float | Decimal) -> Decimal:
    """把外部数值安全地转成 Decimal。

    ``float`` 先转 ``str`` 再转 Decimal，避免二进制浮点误差被带进账本。
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    return Decimal(value)


def _quantum(step: Decimal) -> Decimal:
    """从步长推导指数，例如 ``0.001`` -> ``Decimal("0.001")``。"""
    if step <= 0:
        raise ValueError("步长必须为正数")
    exponent = step.normalize().as_tuple().exponent
    if exponent >= 0:
        return Decimal(1)
    return Decimal(1).scaleb(exponent)


def round_to_step(value: Decimal, step: Decimal, rounding: Rounding = Rounding.NEAREST) -> Decimal:
    """把数值对齐到步长。

    Args:
        value: 原始数值。
        step: 步长（tick size 或 qty step）。
        rounding: 舍入方向。

    Returns:
        对齐后的数值。步长为 1 时返回整数刻度值。
    """
    if step <= 0:
        raise ValueError("步长必须为正数")
    quantum = _quantum(step)
    scaled = value / step
    match rounding:
        case Rounding.DOWN:
            result = scaled.to_integral_value(rounding=ROUND_DOWN)
        case Rounding.UP:
            result = scaled.to_integral_value(rounding=ROUND_CEILING)
        case Rounding.HALF_UP:
            result = scaled.to_integral_value(rounding=ROUND_HALF_UP)
        case Rounding.NEAREST:
            result = scaled.to_integral_value(rounding=ROUND_HALF_UP)
        case _:
            result = scaled.to_integral_value(rounding=ROUND_HALF_UP)
    return (result * step).quantize(quantum)


def quantize_price(value: Decimal, tick_size: Decimal, *, up: bool = False) -> Decimal:
    """价格对齐到 tick size。

    限价买入用 ``up=False``（不高于预算），卖出用 ``up=True``（不低于底价）。
    """
    return round_to_step(value, tick_size, Rounding.UP if up else Rounding.DOWN)


def quantize_qty(value: Decimal, qty_step: Decimal, *, up: bool = False) -> Decimal:
    """数量对齐到 qty step。

    买入用 ``up=False``（不扩大支出）；卖出用 ``up=True``（尽量把持仓清干净，
    但仍受可平量约束）。
    """
    return round_to_step(value, qty_step, Rounding.UP if up else Rounding.DOWN)


def is_multiple_of(value: Decimal, step: Decimal) -> bool:
    """是否已落在步长刻度上（不做舍入）。"""
    if step <= 0:
        raise ValueError("步长必须为正数")
    scaled = value / step
    return scaled == scaled.to_integral_value()


def floor_to_increment(value: Decimal, increment: Decimal) -> Decimal:
    """向下取到 ``increment`` 的整数倍（用于行情深度档位）。"""
    if increment <= 0:
        raise ValueError("increment 必须为正数")
    return (value // increment) * increment


def dec_str(value: Decimal) -> str:
    """去掉尾随零的字符串表示，用于导出与日志。"""
    normalized = value.normalize()
    sign, digits, exponent = normalized.as_tuple()
    if isinstance(exponent, int) and exponent > 0:
        return str(normalized.quantize(_ZERO))
    return format(normalized, "f")


def pct(bps: Decimal) -> Decimal:
    """基点转比例（10000 bps = 100%）。"""
    return bps / Decimal(10000)


def apply_spread(mid: Decimal, spread_bps: Decimal) -> tuple[Decimal, Decimal]:
    """由中价与价差基点推出 (买价, 卖价)。"""
    half = pct(spread_bps) / 2
    return mid * (Decimal(1) - half), mid * (Decimal(1) + half)


def is_zero(value: Decimal, *, epsilon: Decimal = Decimal("1e-12")) -> bool:
    """零碎余额判断（SPT-02）。"""
    return abs(value) < epsilon


def min_decimal(*values: Decimal) -> Decimal:
    """取最小值，忽略 ``None``。"""
    filtered = [v for v in values if v is not None]
    if not filtered:
        raise ValueError("至少需要一个值")
    return min(filtered)


__all__ = [
    "Rounding",
    "apply_spread",
    "dec",
    "dec_str",
    "floor_to_increment",
    "is_multiple_of",
    "is_zero",
    "min_decimal",
    "pct",
    "quantize_price",
    "quantize_qty",
    "round_to_step",
]
