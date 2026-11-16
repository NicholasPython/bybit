"""估值政策与金额换算的公共契约；不依赖账户、数据库或行情传输。

政策指定价格来源，调用方负责验证报价单位、时效、方向及可见数量。
标记权益与按盘口平仓后的损益应使用不同版本的政策，不能混算。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

_ONE = Decimal("1")


@dataclass(frozen=True)
class ValuationPolicy:
    """版本随估值记录保存；沿用控制台的公开接口。"""

    version: str
    currency: str
    day_boundary_tz: str = "Asia/Shanghai"
    option_price_field: str = "mark_price"
    fx_source: str = "spot_mid"

    def __post_init__(self) -> None:
        if not self.version:
            raise ValueError("估值政策必须有版本号")
        if not self.currency:
            raise ValueError("估值政策必须有估值币种")


def option_value(
    *, price: Decimal, qty: Decimal, multiplier: Decimal = _ONE, rate: Decimal = _ONE
) -> Decimal:
    """已选价格对应的期权金额；不自动选价格、不扣费、不推定成交。

    qty 是绝对数量；多空方向由账本处理。零价格可用于已核对的零价值，
    缺价不能传零代替。乘数及汇率必须为正的有限数。
    """
    for name, value in (("价格", price), ("数量", qty)):
        if not value.is_finite() or value < 0:
            raise ValueError(f"{name} 必须是非负有限数")
    for name, value in (("合约乘数", multiplier), ("汇率", rate)):
        if not value.is_finite() or value <= 0:
            raise ValueError(f"{name} 必须是正的有限数")
    return price * qty * multiplier * rate
