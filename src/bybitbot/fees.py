"""手续费估算口径（AC-25 / EXE-03）。

Bybit 期权手续费按**标的指数价**计收，并按权利金封顶：

.. code-block:: text

    fee = min(index_price × underlying_rate, option_price × premium_cap_rate) × qty

默认 ``underlying_rate = 0.03%``（非 VIP taker）、``premium_cap_rate = 7%``。
账户层级可能改变费率；本模块只用于**事前估算**（仓位、预算、止盈判断），
实际成交一律以交易所回报的费用入账（LED-01）。

方向约定
--------
``premium_cap_rate`` 对应公式的上界：拿不到标的指数价时按它计费，
属于保守方向 —— 宁可高估成本，也不要把仓位算大或把收益算高。

现货 taker 费用按成交额计收（``spot_taker_rate``），与期权费用口径分开。
费率是**估算**而不是事实，任何依赖它做出的决定都要在成交后用交易所
实际费用核对。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from bybitbot.money import dec

#: Bybit 期权非 VIP taker：标的指数价的 0.03%。
OPTION_UNDERLYING_RATE = Decimal("0.0003")
#: 期权费用封顶：权利金的 7%。
OPTION_PREMIUM_CAP_RATE = Decimal("0.07")


@dataclass(frozen=True)
class FeeModel:
    """版本化的手续费估算口径。

    ``version`` 随快照与报告一起记录：口径变了，历史结果不可比。
    """

    version: str
    #: 现货 taker 费率（按成交额）。
    spot_taker_rate: Decimal
    #: 期权：标的指数价费率。
    option_underlying_rate: Decimal = OPTION_UNDERLYING_RATE
    #: 期权：权利金封顶比例。
    option_premium_cap_rate: Decimal = OPTION_PREMIUM_CAP_RATE
    #: 期权每张合约的额外费用。
    option_per_contract: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        if not self.version:
            raise ValueError("费用口径必须有版本号")
        for name in (
            "spot_taker_rate",
            "option_underlying_rate",
            "option_premium_cap_rate",
            "option_per_contract",
        ):
            value = dec(getattr(self, name))
            if not value.is_finite() or value < 0:
                raise ValueError(f"{name} 必须是非负有限数")

    @classmethod
    def conservative_default(cls) -> FeeModel:
        """保守默认口径：期权费用按封顶比例计（公式上界）。"""
        return cls(
            version="bybit-fee-v1",
            spot_taker_rate=Decimal("0.001"),
        )

    # ------------------------------------------------------------------
    # 现货
    # ------------------------------------------------------------------
    def spot_fee(self, *, price: Decimal, qty: Decimal) -> Decimal:
        """现货 taker 费用（按成交额计收）。"""
        return dec(price) * dec(qty) * dec(self.spot_taker_rate)

    def spot_fee_ratio(self) -> Decimal:
        """现货费用占成交额的比例。"""
        return dec(self.spot_taker_rate)

    # ------------------------------------------------------------------
    # 期权
    # ------------------------------------------------------------------
    def option_fee(
        self,
        *,
        option_price: Decimal,
        qty: Decimal,
        index_price: Decimal | None = None,
    ) -> Decimal:
        """期权费用（结算币）。

        ``index_price`` 是标的指数价；缺失时按封顶比例计费（保守上界）。
        """
        price, size = dec(option_price), dec(qty)
        if any(not value.is_finite() or value < 0 for value in (price, size)):
            raise ValueError("期权价格与数量必须为非负有限数")
        cap = price * dec(self.option_premium_cap_rate)
        index = dec(index_price) if index_price is not None else None
        if index is None or not index.is_finite() or index <= 0:
            per_unit = cap
        else:
            per_unit = min(index * dec(self.option_underlying_rate), cap)
        return size * (per_unit + dec(self.option_per_contract))

    def option_fee_ratio(
        self,
        *,
        option_price: Decimal,
        index_price: Decimal | None = None,
    ) -> Decimal:
        """期权费用占权利金的比例，用于把预算换算成数量。

        缺失或无效指数价时返回封顶费用比例，包含每张额外费用。
        ``option_price`` 必须为正的有限数。
        """
        price = dec(option_price)
        if not price.is_finite() or price <= 0:
            raise ValueError("费用比例需要正的有限期权价格")
        return (
            self.option_fee(option_price=price, qty=Decimal("1"), index_price=index_price) / price
        )


__all__ = [
    "OPTION_PREMIUM_CAP_RATE",
    "OPTION_UNDERLYING_RATE",
    "FeeModel",
]
