"""配对级硬风控闸门（PRD FR-03）。

风控是**独立中间件**：策略层无权绕过，本模块也只输出结论与理由，
不写库、不调交易所 —— 与 :mod:`bybitbot.risk.engine` 同一口径，
便于回放与审计。

一票否决的判定顺序不重要：**所有条目都要给出结论**，一条不满足即整体拒绝。
静默通过一条、悄悄拒绝另一条，是风控最危险的失效方式。

覆盖的风控维度：

* ``FR-03.1`` 单腿裸敞口熔断 —— 名义差超过上限并持续超过宽限期即强平多余敞口；
* ``FR-03.2`` 价差极端偏离止损 —— ``|Z|`` 越过硬阈值即双边止损；
* ``FR-03.3`` 账户级保证金与杠杆 —— 单组占净值比例、总杠杆、维持保证金率；
* ``FR-03.4`` 资金费率逆风保护 —— 净费率支出吞掉预期利润的一半即提前了结；
* ``FR-03.5`` API 异常与死循环防护 —— 发单频率与全局冷却。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

#: 拒绝原因码（写进审计与告警，页面据此渲染，不回显自由文本）。
MM_RATE_TOO_HIGH = "MM_RATE_TOO_HIGH"
FORCED_REDUCE_REQUIRED = "FORCED_REDUCE_REQUIRED"
PAIR_EQUITY_RATIO_EXCEEDED = "PAIR_EQUITY_RATIO_EXCEEDED"
ACCOUNT_LEVERAGE_EXCEEDED = "ACCOUNT_LEVERAGE_EXCEEDED"
PAIR_NOTIONAL_CAP_EXCEEDED = "PAIR_NOTIONAL_CAP_EXCEEDED"
API_COOLDOWN_ACTIVE = "API_COOLDOWN_ACTIVE"
ORDER_RATE_EXCEEDED = "ORDER_RATE_EXCEEDED"
UNHEDGE_EXPOSURE_BREACH = "UNHEDGE_EXPOSURE_BREACH"
SPREAD_STOP_LOSS = "SPREAD_STOP_LOSS"
FUNDING_PRESSURE = "FUNDING_PRESSURE"


@dataclass(frozen=True)
class PairRiskLimits:
    """配对级风控限额。默认值来自 PRD FR-03 的具体数值。"""

    #: 单个套利对最大占用名义本金占账户净值的比例（FR-03.3：30%）。
    max_pair_equity_ratio: Decimal = Decimal("0.30")
    #: 全账户总名义敞口杠杆上限（FR-03.3：3x）。
    max_account_leverage: Decimal = Decimal("3")
    #: 维持保证金率超过该值禁止一切新开仓（FR-03.3：50%）。
    max_open_mm_rate: Decimal = Decimal("0.50")
    #: 维持保证金率超过该值触发全账户等比例减仓（FR-03.3：75%）。
    force_reduce_mm_rate: Decimal = Decimal("0.75")
    #: 单腿裸敞口名义上限（FR-03.1：50 USDT）。
    max_unhedged_notional: Decimal = Decimal("50")
    #: 裸敞口超过上限后允许持续的宽限时间（FR-03.1：5 秒）。
    unhedged_grace_ms: int = 5000
    #: 价差硬止损阈值（FR-03.2：3.5σ）。
    spread_stop_z: Decimal = Decimal("3.5")
    #: 资金费率净支出占预期利润的比例上限（FR-03.4：50%）。
    max_funding_share: Decimal = Decimal("0.5")
    #: 同一配对 10 秒内的发单/撤单次数上限（FR-03.5：15 次）。
    max_orders_per_window: int = 15
    #: 订单事件统计窗口（毫秒，FR-03.5：10 秒）。
    order_window_ms: int = 10_000
    #: 交易所限频/超时后的全局冷却（FR-03.5：30 秒）。
    api_cooldown_ms: int = 30_000


@dataclass(frozen=True)
class PairRiskContext:
    """判定时的账户与运行上下文。"""

    #: 风控权益（USDT）。
    equity: Decimal
    #: 当前全账户名义敞口。
    account_notional: Decimal
    #: 账户维持保证金率（0—1）。
    mm_rate: Decimal
    #: 窗口内该配对的发单/撤单次数。
    order_events: int = 0
    #: 全局冷却解除时刻（毫秒时间戳）。
    api_cooldown_until_ms: int = 0
    #: 当前时刻（毫秒时间戳）。
    now_ms: int = 0


@dataclass(frozen=True)
class PairRiskDecision:
    """风控判定结论。``allowed`` 为假时 ``reasons`` 一定非空。"""

    allowed: bool
    reasons: tuple[str, ...] = ()
    forced_reduce: bool = False

    @property
    def reason_text(self) -> str:
        return "、".join(self.reasons)


def evaluate_open(
    *,
    limits: PairRiskLimits,
    context: PairRiskContext,
    pair_notional: Decimal,
    pair_notional_cap: Decimal | None = None,
) -> PairRiskDecision:
    """新开仓前的闸门判定。

    Args:
        limits: 风控限额。
        context: 账户与运行上下文。
        pair_notional: 本次拟开仓的名义价值。
        pair_notional_cap: 该配对的绝对名义上限；``None`` 表示不限。

    Returns:
        判定结论。任何一条不满足即拒绝，并列出全部原因。
    """
    reasons: list[str] = []
    if context.equity <= 0:
        return PairRiskDecision(False, ("EQUITY_UNAVAILABLE",))
    if context.mm_rate > limits.max_open_mm_rate:
        reasons.append(MM_RATE_TOO_HIGH)
    if pair_notional <= 0:
        reasons.append("PAIR_NOTIONAL_INVALID")
    if pair_notional > context.equity * limits.max_pair_equity_ratio:
        reasons.append(PAIR_EQUITY_RATIO_EXCEEDED)
    if context.account_notional + pair_notional > context.equity * limits.max_account_leverage:
        reasons.append(ACCOUNT_LEVERAGE_EXCEEDED)
    if pair_notional_cap is not None and pair_notional > pair_notional_cap:
        reasons.append(PAIR_NOTIONAL_CAP_EXCEEDED)
    if context.api_cooldown_until_ms > context.now_ms:
        reasons.append(API_COOLDOWN_ACTIVE)
    if context.order_events > limits.max_orders_per_window:
        reasons.append(ORDER_RATE_EXCEEDED)
    forced = context.mm_rate > limits.force_reduce_mm_rate
    return PairRiskDecision(not reasons, tuple(reasons), forced)


def evaluate_forced_reduce(*, limits: PairRiskLimits, context: PairRiskContext) -> bool:
    """是否触发全账户等比例减仓（FR-03.3）。"""
    return context.mm_rate > limits.force_reduce_mm_rate


def unhedged_breach(
    *,
    limits: PairRiskLimits,
    notional_a: Decimal,
    notional_b: Decimal,
    first_breach_ms: int | None,
    now_ms: int,
) -> bool:
    """单腿裸敞口是否已经越界并超过宽限期（FR-03.1）。

    Args:
        first_breach_ms: 首次越界的时刻；``None`` 表示当前不在越界状态。
        now_ms: 当前时刻。
    """
    if abs(notional_a - notional_b) <= limits.max_unhedged_notional:
        return False
    if first_breach_ms is None:
        return False
    return now_ms - first_breach_ms >= limits.unhedged_grace_ms


def evaluate_spread_stop(*, limits: PairRiskLimits, z_value: Decimal) -> bool:
    """价差是否越过硬止损阈值（FR-03.2）。"""
    return abs(z_value) >= limits.spread_stop_z


def evaluate_funding_pressure(
    *,
    limits: PairRiskLimits,
    net_funding_pnl: Decimal,
    expected_profit: Decimal,
) -> bool:
    """资金费率是否已经吞掉预期利润的一半（FR-03.4）。

    ``net_funding_pnl`` 为负表示净支出。
    """
    if expected_profit <= 0:
        return False
    return -net_funding_pnl >= expected_profit * limits.max_funding_share


__all__ = [
    "ACCOUNT_LEVERAGE_EXCEEDED",
    "API_COOLDOWN_ACTIVE",
    "FUNDING_PRESSURE",
    "FORCED_REDUCE_REQUIRED",
    "MM_RATE_TOO_HIGH",
    "ORDER_RATE_EXCEEDED",
    "PAIR_EQUITY_RATIO_EXCEEDED",
    "PAIR_NOTIONAL_CAP_EXCEEDED",
    "SPREAD_STOP_LOSS",
    "UNHEDGE_EXPOSURE_BREACH",
    "PairRiskContext",
    "PairRiskDecision",
    "PairRiskLimits",
    "evaluate_forced_reduce",
    "evaluate_funding_pressure",
    "evaluate_open",
    "evaluate_spread_stop",
    "unhedged_breach",
]
