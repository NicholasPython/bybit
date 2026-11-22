"""配对状态机与对冲量计算（PRD FR-02 / FR-03）。

配对生命周期::

    OPENING ──双腿建立──> HEDGED ──发起平仓──> CLOSING ──双腿了结──> CLOSED
       │                      │                    │
       │                      │                    └──裸腿无法修复──> ERROR_UNHEDGED
       ├──Leg A 超时零成交──> ABORTED
       └──裸腿无法修复────────> ERROR_UNHEDGED

三条硬不变量（与 :mod:`bybitbot.execution.states` 同口径，写在最前面是因为
它们决定了"什么情况下允许继续"）：

    1. **终态不可倒退** —— ``CLOSED`` / ``ABORTED`` / ``ERROR_UNHEDGED``
       之后不再迁移；晚到的事实只更新账务，不改生命周期。
    2. **裸敞口有界** —— 进入 ``HEDGED`` 时两条腿的名义价值差不得超过
       配置上限；越界的配对只能进入 ``ERROR_UNHEDGED``，不能停在"已对冲"。
    3. **成交数量单调不减** —— 晚到的真实成交仍然入账，不能被后来的
       快照或重算结果覆盖成更小的数量（与 :mod:`bybitbot.execution.states`
       同口径：旧快照不倒退已确认的成交）。

本模块只做纯计算：不写库、不调交易所、不持有时间。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation

#: 配对状态。
OPENING = "OPENING"
HEDGED = "HEDGED"
CLOSING = "CLOSING"
CLOSED = "CLOSED"
ABORTED = "ABORTED"
ERROR_UNHEDGED = "ERROR_UNHEDGED"

#: 终态集合。
TERMINAL_STATES: frozenset[str] = frozenset({CLOSED, ABORTED, ERROR_UNHEDGED})

#: 合法迁移表。
_TRANSITIONS: dict[str, frozenset[str]] = {
    OPENING: frozenset({HEDGED, ABORTED, ERROR_UNHEDGED}),
    HEDGED: frozenset({CLOSING, ERROR_UNHEDGED}),
    CLOSING: frozenset({CLOSED, ERROR_UNHEDGED}),
    CLOSED: frozenset({CLOSED}),
    ABORTED: frozenset({ABORTED}),
    ERROR_UNHEDGED: frozenset({ERROR_UNHEDGED}),
}


class TransitionError(Exception):
    """非法状态迁移，或违反配对级不变量。"""


@dataclass(frozen=True)
class PairState:
    """配对的业务状态与两条腿的已确认成交事实。"""

    state: str
    filled_qty_a: Decimal = Decimal("0")
    filled_qty_b: Decimal = Decimal("0")
    notional_a: Decimal = Decimal("0")
    notional_b: Decimal = Decimal("0")
    avg_price_a: Decimal | None = None
    avg_price_b: Decimal | None = None

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def both_legs_established(self) -> bool:
        """两条腿都已有成交 —— 只有此时才谈得上"已对冲"。"""
        return self.filled_qty_a > 0 and self.filled_qty_b > 0


def can_transition(current: str, target: str) -> bool:
    """判断是否允许从 ``current`` 迁移到 ``target``。"""
    if current == target:
        return True
    return target in _TRANSITIONS.get(current, frozenset())


def unhedged_notional(state: PairState) -> Decimal:
    """两条腿的名义价值差 —— 裸敞口的量度。"""
    return abs(state.notional_a - state.notional_b)


def apply_transition(
    state: PairState,
    *,
    target: str,
    filled_qty_a: Decimal | None = None,
    filled_qty_b: Decimal | None = None,
    notional_a: Decimal | None = None,
    notional_b: Decimal | None = None,
    avg_price_a: Decimal | None = None,
    avg_price_b: Decimal | None = None,
    max_unhedged_notional: Decimal | None = None,
) -> PairState:
    """应用一次配对状态迁移。

    Args:
        state: 当前状态。
        target: 目标状态。
        filled_qty_a / filled_qty_b: 交易所口径的**累计**成交量。
        notional_a / notional_b: 两条腿的累计名义价值。
        avg_price_a / avg_price_b: 成交均价。
        max_unhedged_notional: 裸敞口上限；进入 ``HEDGED`` 时必须校验。

    Raises:
        TransitionError: 非法迁移，或违反「成交数量单调不减」「裸敞口有界」。
    """
    if target not in _TRANSITIONS:
        raise TransitionError(f"未知配对状态 {target}")

    new_qty_a = (
        state.filled_qty_a if filled_qty_a is None else max(state.filled_qty_a, filled_qty_a)
    )
    new_qty_b = (
        state.filled_qty_b if filled_qty_b is None else max(state.filled_qty_b, filled_qty_b)
    )
    new_notional_a = state.notional_a if notional_a is None else notional_a
    new_notional_b = state.notional_b if notional_b is None else notional_b

    if state.is_terminal:
        # 终态不可倒退，但晚到的成交事实仍然入账。
        return replace(
            state,
            filled_qty_a=new_qty_a,
            filled_qty_b=new_qty_b,
            notional_a=new_notional_a,
            notional_b=new_notional_b,
            avg_price_a=avg_price_a or state.avg_price_a,
            avg_price_b=avg_price_b or state.avg_price_b,
        )

    if not can_transition(state.state, target):
        raise TransitionError(f"非法迁移 {state.state} → {target}")

    candidate = replace(
        state,
        state=target,
        filled_qty_a=new_qty_a,
        filled_qty_b=new_qty_b,
        notional_a=new_notional_a,
        notional_b=new_notional_b,
        avg_price_a=avg_price_a or state.avg_price_a,
        avg_price_b=avg_price_b or state.avg_price_b,
    )

    if target == HEDGED:
        if not candidate.both_legs_established:
            raise TransitionError("两条腿都未建立成交，不能标记为已对冲")
        if (
            max_unhedged_notional is not None
            and unhedged_notional(candidate) > max_unhedged_notional
        ):
            raise TransitionError("裸敞口超出上限，不能标记为已对冲")

    return candidate


def quantize_qty(qty: Decimal, step: Decimal) -> Decimal:
    """把数量向下取整到步长。

    向下是安全方向：宁可少对冲一点，也不要多出一条敞口。
    """
    if step <= 0:
        raise ValueError("数量步长必须为正")
    if qty <= 0:
        return Decimal("0")
    return (qty / step).to_integral_value(rounding="ROUND_FLOOR") * step


def equal_notional_qty(
    *,
    filled_notional_a: Decimal,
    price_b: Decimal,
    qty_step_b: Decimal,
) -> Decimal:
    """按等名义价值原则计算对冲腿数量（PRD FR-02.2）。

    ``qty_b = 名义价值 / price_b``，再向下取整到腿 B 的数量步长。
    """
    if price_b <= 0:
        raise ValueError("对冲腿价格必须为正")
    if filled_notional_a <= 0:
        return Decimal("0")
    return quantize_qty(filled_notional_a / price_b, qty_step_b)


def log_spread(price_a: Decimal, price_b: Decimal, hedge_ratio: Decimal) -> Decimal:
    """对数价差 ``ln(price_a) - β · ln(price_b)``。

    统计套利用对数价差保证协整关系在价格水平变化下仍然稳定。
    """
    if price_a <= 0 or price_b <= 0:
        raise ValueError("价格必须为正")
    if hedge_ratio <= 0:
        raise ValueError("对冲系数必须为正")
    return price_a.ln() - hedge_ratio * price_b.ln()


def z_score(value: Decimal, mean: Decimal, stdev: Decimal) -> Decimal:
    """偏离度 ``Z = (S - μ) / σ``。"""
    if stdev <= 0:
        raise ValueError("标准差必须为正")
    return (value - mean) / stdev


def slippage_bps(reference: Decimal, actual: Decimal) -> Decimal:
    """成交价相对参考价的滑点（基点）。"""
    if reference <= 0:
        raise ValueError("参考价必须为正")
    return (actual - reference).copy_abs() / reference * Decimal(10000)


def parse_decimal(value: str | Decimal | int) -> Decimal:
    """把外部文本解析成 Decimal，拒绝无效数值。"""
    try:
        result = Decimal(value)
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f"无法解析为数值：{value!r}") from None
    if not result.is_finite():
        raise ValueError(f"数值必须有限：{value!r}")
    return result


__all__ = [
    "ABORTED",
    "CLOSED",
    "CLOSING",
    "ERROR_UNHEDGED",
    "HEDGED",
    "OPENING",
    "TERMINAL_STATES",
    "PairState",
    "TransitionError",
    "apply_transition",
    "can_transition",
    "equal_notional_qty",
    "log_spread",
    "parse_decimal",
    "quantize_qty",
    "slippage_bps",
    "unhedged_notional",
    "z_score",
]
