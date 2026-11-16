"""Bybit 费用口径（公共层）。

期权手续费按**标的指数价**计收，并按**权利金**封顶::

    fee = qty × (min(index_price × underlying_rate, premium × premium_cap_rate)
                 + per_contract)

指数价缺失时按封顶比例计费 —— 这是公式的上界，属于**保守方向**：宁可高估
成本，也不要把仓位算大或把收益算高。

现货 taker 费用按成交额计收，与期权费用口径分开。

这一层只做**事前估算**（预算、仓位、止盈判断）；真实成交一律以交易所回报
的费用入账。费率是估算而不是事实，任何依赖它做出的决定都要在成交后用
交易所实际费用核对。

控制台与研究侧共用这里的公式。此前两边各抄一份（控制台
``bybitbot.fees``、研究脚本内联函数），参数改一处就会让两边的账本对不上。
"""

from __future__ import annotations

from decimal import Decimal

_ZERO = Decimal("0")


def _non_negative(value: Decimal, name: str) -> Decimal:
    if not value.is_finite() or value < 0:
        raise ValueError(f"{name} 必须是非负有限数")
    return value


def option_fee(
    *,
    premium: Decimal,
    qty: Decimal,
    index_price: Decimal | None,
    underlying_rate: Decimal,
    premium_cap_rate: Decimal,
    per_contract: Decimal = _ZERO,
) -> Decimal:
    """期权手续费（结算币）。

    Args:
        premium: 权利金（期权价格）。
        qty: 数量。
        index_price: 标的指数价；``None``、非有限或非正数表示**拿不到**，
            按封顶比例计费。
        underlying_rate: 标的指数价费率。
        premium_cap_rate: 权利金封顶比例。
        per_contract: 每张合约的额外费用。

    Returns:
        费用金额。
    """
    price = _non_negative(premium, "期权价格")
    size = _non_negative(qty, "数量")
    rate = _non_negative(underlying_rate, "标的指数价费率")
    cap_rate = _non_negative(premium_cap_rate, "权利金封顶比例")
    extra = _non_negative(per_contract, "每张额外费用")

    cap = price * cap_rate
    if index_price is None or not index_price.is_finite() or index_price <= 0:
        per_unit = cap
    else:
        per_unit = min(index_price * rate, cap)
    return size * (per_unit + extra)


def spot_fee(*, price: Decimal, qty: Decimal, rate: Decimal) -> Decimal:
    """现货 taker 费用（按成交额计收）。"""
    value = _non_negative(price, "价格")
    size = _non_negative(qty, "数量")
    fee_rate = _non_negative(rate, "费率")
    return value * size * fee_rate


def delivery_fee(
    *,
    price: Decimal,
    strike: Decimal,
    rate: Decimal,
    cap_rate: Decimal,
    qty: Decimal,
) -> Decimal:
    """期权交割手续费。

    交割费按合约价值计收并封顶::

        fee = qty × min(rate × price, cap_rate × max(price - strike, 0))

    Args:
        price: 交割时合约的结算参考价。
        strike: 行权价。
        rate: 价值费率。
        cap_rate: 内在价值封顶比例。
        qty: 数量。
    """
    value = _non_negative(price, "结算参考价")
    level = _non_negative(strike, "行权价")
    fee_rate = _non_negative(rate, "费率")
    cap = _non_negative(cap_rate, "封顶比例")
    size = _non_negative(qty, "数量")
    return size * min(fee_rate * value, cap * max(value - level, _ZERO))


__all__ = [
    "delivery_fee",
    "option_fee",
    "spot_fee",
]
