"""Delta 敞口风控（RSK-05）。

需求原文：
    Delta 风控单位为每个标的的基础币数量，设该标的绝对限额为 ``L``。
    以可信实际持仓计算 ``D0``，将候选订单和全部未完成订单的剩余量转换为
    当前行情下完全成交的 Delta 变化 ``d_i``；检查区间
    ``D_min = D0 + sum(min(0, d_i))``、``D_max = D0 + sum(max(0, d_i))``
    均落在 ``[-L, L]`` 内。不能依靠两张方向相反的挂单同时成交来抵消风险；
    无法量化的未知敞口直接暂停开仓。

单位约定（⚠️ 需要用样例验证 —— RSK-05 明确要求）：
    * 现货：1 个基础币 = 1.0 Delta。
    * 期权：交易所返回的 ``delta`` 是**每张合约**的 Delta；合约乘以数量后
      折算为标的单位。折算系数由 :class:`DeltaUnits` 显式给出 ——
      若交易所返回的已是组合 Delta，则把 ``per_contract_delta`` 置为 1.0
      并关掉 ``multiply_by_qty``。

区间而不是净值的理由：两张方向相反的挂单不能互相抵消风险，
所以负向变化和正向变化分别累加，得到一个保守区间。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from bybitbot.money import dec


@dataclass(frozen=True)
class DeltaUnits:
    """Greeks 单位映射。"""

    #: 交易所 delta 是否为「每张合约」的值。为 False 时视为组合总值。
    multiply_by_qty: bool = True
    #: 每张合约对应多少标的（用于把合约数量折成标的数量）。
    contract_multiplier: Decimal = Decimal("1")
    #: 若交易所 delta 需要除以这个值才得到标的单位（例如按面值计价）。
    divisor: Decimal = Decimal("1")

    def to_underlying(self, delta_per_contract: Decimal, qty: Decimal) -> Decimal:
        """把交易所的 delta 换算成标的单位。"""
        value = dec(delta_per_contract)
        quantity = dec(qty)
        if (
            not value.is_finite()
            or not quantity.is_finite()
            or not self.divisor.is_finite()
            or self.divisor <= 0
            or not self.contract_multiplier.is_finite()
            or self.contract_multiplier <= 0
        ):
            raise ValueError("Delta 单位、数量和映射必须为有限有效值")
        value = value / self.divisor
        if self.multiply_by_qty:
            value = value * quantity
        return value * self.contract_multiplier


@dataclass(frozen=True)
class DeltaCandidate:
    """一个候选/未完成订单的 Delta 变化。

    ``delta_if_filled`` 是该订单**完全成交**时的 Delta 变化。
    """

    symbol: str
    underlying: str
    qty: Decimal
    delta_if_filled: Decimal | None
    order_id: int | None = None
    order_link_id: str | None = None
    #: 剩余数量（部分成交后小于原始数量）。
    leaves_qty: Decimal | None = None

    @property
    def effective_qty(self) -> Decimal:
        return self.leaves_qty if self.leaves_qty is not None else self.qty


@dataclass(frozen=True)
class DeltaCheck:
    """某个标的的 Delta 检查结论。"""

    underlying: str
    d0: Decimal
    d_min: Decimal
    d_max: Decimal
    limit: Decimal
    approved: bool
    reason_code: str = ""
    detail: str = ""
    candidates: list[DeltaCandidate] = field(default_factory=list)

    def as_dict(self) -> dict[str, str]:
        return {
            "underlying": self.underlying,
            "d0": str(self.d0),
            "d_min": str(self.d_min),
            "d_max": str(self.d_max),
            "limit": str(self.limit),
            "approved": str(self.approved),
            "reason_code": self.reason_code,
        }


def spot_delta_change(qty: Decimal, side: str) -> Decimal:
    """现货成交的 Delta 变化（买入为正，卖出为负）。"""
    qty = dec(qty)
    return qty if side.lower() == "buy" else -qty


def option_delta_change(position_delta: Decimal, qty: Decimal, units: DeltaUnits) -> Decimal:
    """期权成交的 Delta 变化。

    买入开仓增加多头 Delta；卖出平仓减少多头 Delta。
    """
    return units.to_underlying(position_delta, qty)


def evaluate_delta_bounds(
    *,
    underlying: str,
    d0: Decimal,
    candidates: list[DeltaCandidate],
    limit: Decimal,
) -> DeltaCheck:
    """按 RSK-05 的区间口径判定。

    Args:
        underlying: 标的。
        d0: 可信实际持仓的 Delta（标的单位）。
        candidates: 候选订单 + 全部未完成订单的剩余量。
        limit: 该标的的绝对 Delta 限额 L。

    Returns:
        :class:`DeltaCheck`。区间任一端越界即拒绝。
    """
    d0 = dec(d0)
    limit = dec(limit)

    if (
        not d0.is_finite()
        or not limit.is_finite()
        or limit <= 0
        or any(
            c.delta_if_filled is None
            or not c.delta_if_filled.is_finite()
            or not c.effective_qty.is_finite()
            or c.effective_qty < 0
            for c in candidates
        )
    ):
        return DeltaCheck(
            underlying=underlying,
            d0=d0,
            d_min=d0,
            d_max=d0,
            limit=limit,
            approved=False,
            reason_code="DELTA_UNQUANTIFIED",
            detail=f"{underlying} 存在未知敞口或无效单位，禁止开仓",
            candidates=list(candidates),
        )
    downs = [min(Decimal("0"), c.delta_if_filled) for c in candidates]
    ups = [max(Decimal("0"), c.delta_if_filled) for c in candidates]
    d_min = d0 + sum(downs, Decimal("0"))
    d_max = d0 + sum(ups, Decimal("0"))

    if d_min < -limit or d_max > limit:
        breached = []
        if d_min < -limit:
            breached.append(f"下界 {d_min} < -{limit}")
        if d_max > limit:
            breached.append(f"上界 {d_max} > {limit}")
        return DeltaCheck(
            underlying=underlying,
            d0=d0,
            d_min=d_min,
            d_max=d_max,
            limit=limit,
            approved=False,
            reason_code="DELTA_LIMIT_BREACH",
            detail=f"{underlying} Delta 区间越界：" + "；".join(breached),
            candidates=list(candidates),
        )

    return DeltaCheck(
        underlying=underlying,
        d0=d0,
        d_min=d_min,
        d_max=d_max,
        limit=limit,
        approved=True,
        candidates=list(candidates),
    )


def delta_of_positions(
    *,
    positions: list[tuple[str, Decimal]],
) -> dict[str, Decimal]:
    """汇总可信持仓的 Delta。

    Args:
        positions: ``(标的, Delta)`` 列表，只应包含可信持仓。
    """
    total: dict[str, Decimal] = {}
    for underlying, delta in positions:
        total[underlying] = total.get(underlying, Decimal("0")) + dec(delta)
    return total


__all__ = [
    "DeltaCandidate",
    "DeltaCheck",
    "DeltaUnits",
    "delta_of_positions",
    "evaluate_delta_bounds",
    "option_delta_change",
    "spot_delta_change",
]
