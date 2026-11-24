"""双腿价差的滑动窗口信号计算（阶段三，纯函数层）。

输入是一段按时间升序排列的价格样本，输出是带理由的结构化决策。
本模块不写库、不调交易所、不持有时间，所有判定都可以用同一组样本复算，
供后续 worker 接入并留痕审计。

判定顺序：

    样本有效性 → 窗口长度 → 时间单调性 → 数据新鲜度 → 价差与统计量
    → 硬止损 → 开仓阈值与成本过滤 → 平仓 → 无信号

仅在开仓时要求预期回归收益覆盖双边费用、滑点与资金费率缓冲。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from bybitbot.spread.states import log_spread

_ZERO = Decimal(0)
_BPS_DENOMINATOR = Decimal(10000)

# 动作码（稳定英文，供 worker 与审计日志消费）。
ACTION_NONE = "NONE"
ACTION_OPEN = "OPEN"
ACTION_CLOSE = "CLOSE"
ACTION_STOP = "STOP"

# 方向码：A 高估则做空 A、做多 B；A 低估则反向。
DIRECTION_SHORT_A_LONG_B = "SHORT_A_LONG_B"
DIRECTION_LONG_A_SHORT_B = "LONG_A_SHORT_B"

# 理由码（enum-like 字符串，不回显自由文本）。
REASON_COST_FILTER_NOT_PASSED = "COST_FILTER_NOT_PASSED"
REASON_STOP_LOSS_TRIGGERED = "STOP_LOSS_TRIGGERED"
REASON_ENTRY_THRESHOLD_REACHED = "ENTRY_THRESHOLD_REACHED"
REASON_EXIT_THRESHOLD_REACHED = "EXIT_THRESHOLD_REACHED"
REASON_NO_SIGNAL = "NO_SIGNAL"


class SignalUnavailable(Exception):
    """样本不足、损坏、过期或统计量退化，无法给出可信信号。"""


def _decimal(value: object, name: str) -> Decimal:
    """把 Decimal/int 统一成有限 Decimal，其余类型一律拒绝。"""
    if isinstance(value, bool) or not isinstance(value, (Decimal, int)):
        raise ValueError(f"{name} 必须是有限 Decimal")
    result = value if isinstance(value, Decimal) else Decimal(value)
    if not result.is_finite():
        raise ValueError(f"{name} 必须是有限 Decimal")
    return result


def _nonnegative(value: object, name: str) -> Decimal:
    result = _decimal(value, name)
    if result < _ZERO:
        raise ValueError(f"{name} 不能为负")
    return result


def _positive_finite(value: object, name: str) -> Decimal:
    result = _decimal(value, name)
    if result <= _ZERO:
        raise ValueError(f"{name} 必须为正")
    return result


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} 必须是整数")
    return value


@dataclass(frozen=True)
class PriceSample:
    """某一时刻两条腿的快照。

    ``funding_a`` / ``funding_b`` 是当次采样的资金费率，允许为负（收取）；
    价格必须是有限正数，时间戳必须是非负整数毫秒。
    """

    at_ms: int
    price_a: Decimal
    price_b: Decimal
    funding_a: Decimal = 0
    funding_b: Decimal = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "at_ms", _integer(self.at_ms, "at_ms"))
        if self.at_ms < 0:
            raise ValueError("at_ms 不能为负")
        object.__setattr__(self, "price_a", _positive_finite(self.price_a, "price_a"))
        object.__setattr__(self, "price_b", _positive_finite(self.price_b, "price_b"))
        object.__setattr__(self, "funding_a", _decimal(self.funding_a, "funding_a"))
        object.__setattr__(self, "funding_b", _decimal(self.funding_b, "funding_b"))


@dataclass(frozen=True)
class SignalPolicy:
    """滑动窗口与阈值口径。

    ``exit_z <= entry_z < stop_z``：退出带内只平仓，中间地带不动作，
    超过硬止损阈值必须止损。``fee_rate`` 为单边费率，``slippage_bps`` 为
    单边基点滑点，两者都按双腿计。
    """

    window: int
    entry_z: Decimal
    exit_z: Decimal
    stop_z: Decimal
    fee_rate: Decimal
    slippage_bps: Decimal
    funding_buffer: Decimal = 0
    max_signal_age_ms: int = 30000

    def __post_init__(self) -> None:
        object.__setattr__(self, "window", _integer(self.window, "window"))
        if self.window < 2:
            raise ValueError("window 必须至少为 5")
        for name in ("entry_z", "exit_z", "stop_z", "fee_rate", "slippage_bps"):
            object.__setattr__(self, name, _decimal(getattr(self, name), name))
        object.__setattr__(
            self, "funding_buffer", _nonnegative(self.funding_buffer, "funding_buffer")
        )
        for name in ("fee_rate", "slippage_bps"):
            if getattr(self, name) < _ZERO:
                raise ValueError(f"{name} 不能为负")
        if self.exit_z < _ZERO:
            raise ValueError("exit_z 不能为负")
        if not self.exit_z < self.entry_z < self.stop_z:
            raise ValueError("阈值必须满足 exit_z < entry_z < stop_z")
        object.__setattr__(
            self, "max_signal_age_ms", _integer(self.max_signal_age_ms, "max_signal_age_ms")
        )
        if self.max_signal_age_ms < 0:
            raise ValueError("max_signal_age_ms 不能为负")


@dataclass(frozen=True)
class SignalDecision:
    """一次信号判定结果，所有数值字段都随决策留痕。"""

    action: str
    z: Decimal
    spread: Decimal
    mean: Decimal
    stdev: Decimal
    expected_cost: Decimal
    reason: str
    direction: str = ""


def log_spread_value(sample: PriceSample, hedge_ratio: Decimal) -> Decimal:
    """计算单个样本的对数价差 ``ln(price_a) - β·ln(price_b)``。

    Args:
        sample: 双腿价格快照。
        hedge_ratio: 对冲系数 β，必须为正有限值。

    Raises:
        ValueError: β 非正或非有限。
    """
    beta = _positive_finite(hedge_ratio, "hedge_ratio")
    return log_spread(sample.price_a, sample.price_b, beta)


def rolling_stats(values: Sequence[Decimal], window: int) -> tuple[Decimal, Decimal]:
    """最近 ``window`` 个值的均值与总体标准差。

    标准差用 ``Decimal.sqrt()`` 计算，避免浮点误差进入审计口径。

    Args:
        values: 按时间升序排列的数值序列。
        window: 窗口长度，必须至少为 2；只取最后 ``window`` 个值。

    Returns:
        ``(mean, stdev)``，两者都是有限 Decimal，且 ``stdev > 0``。

    Raises:
        ValueError: ``window`` 非法。
        SignalUnavailable: 样本不足、数值非有限或标准差退化。
    """
    size = _integer(window, "window")
    if size < 2:
        raise ValueError("window 必须至少为 5")
    if isinstance(values, (str, bytes)):
        raise SignalUnavailable("样本序列无效")
    tail = list(values)
    if len(tail) < size:
        raise SignalUnavailable("样本不足")
    tail = tail[-size:]
    numbers: list[Decimal] = []
    for value in tail:
        if isinstance(value, bool) or not isinstance(value, (Decimal, int)):
            raise SignalUnavailable("样本值无效")
        number = value if isinstance(value, Decimal) else Decimal(value)
        if not number.is_finite():
            raise SignalUnavailable("样本值无效")
        numbers.append(number)
    mean = sum(numbers, _ZERO) / Decimal(size)
    variance = sum(((value - mean) ** 2 for value in numbers), _ZERO) / Decimal(size)
    stdev = variance.sqrt()
    if not stdev.is_finite() or stdev <= _ZERO:
        raise SignalUnavailable("标准差退化")
    return mean, stdev


def expected_cost(
    notional: Decimal,
    fee_rate: Decimal,
    slippage_bps: Decimal,
    funding_buffer: Decimal,
) -> Decimal:
    """双边交易摩擦成本。

    ``2·notional·fee_rate + 2·notional·slippage_bps/10000 + funding_buffer``。
    资金费率缓冲已经折算成金额，不再乘名义价值。

    Raises:
        ValueError: 任一输入非有限或为负。
    """
    principal = _nonnegative(notional, "notional")
    fee = _nonnegative(fee_rate, "fee_rate")
    slippage = _nonnegative(slippage_bps, "slippage_bps")
    buffer = _nonnegative(funding_buffer, "funding_buffer")
    return principal * fee * 2 + principal * slippage * 2 / _BPS_DENOMINATOR + buffer


def evaluate_signal(
    samples: Sequence[PriceSample],
    policy: SignalPolicy,
    *,
    hedge_ratio: Decimal,
    notional: Decimal,
    now_ms: int,
) -> SignalDecision:
    """按滑动窗口判定双腿价差信号。

    Args:
        samples: 按时间严格递增的样本序列。
        policy: 窗口、阈值与成本口径。
        hedge_ratio: 对冲系数 β。
        notional: 用于估算收益与成本的名义价值，必须非负有限。
        now_ms: 判定时刻（毫秒时间戳）。

    Returns:
        :class:`SignalDecision`。数据不可用时抛 :class:`SignalUnavailable`；
        参数本身非法时抛 :class:`ValueError`。

    Raises:
        SignalUnavailable: 样本不足、时间非严格递增、数据过期或标准差退化。
        ValueError: 策略以外的数值参数非法。
    """
    size = policy.window
    beta = _positive_finite(hedge_ratio, "hedge_ratio")
    principal = _nonnegative(notional, "notional")
    reference_ms = _integer(now_ms, "now_ms")

    if isinstance(samples, (str, bytes)):
        raise SignalUnavailable("样本序列无效")
    buffer = list(samples)
    if len(buffer) < size:
        raise SignalUnavailable("样本不足")
    previous_ms: int | None = None
    for sample in buffer:
        at_ms = sample.at_ms
        if _integer(at_ms, "at_ms") != at_ms or at_ms < 0:
            raise SignalUnavailable("样本时间无效")
        if previous_ms is not None and at_ms <= previous_ms:
            raise SignalUnavailable("样本时间非严格递增")
        previous_ms = at_ms

    if reference_ms - buffer[-1].at_ms > policy.max_signal_age_ms:
        raise SignalUnavailable("样本过期")

    spreads = [log_spread(sample.price_a, sample.price_b, beta) for sample in buffer]
    current = spreads[-1]
    mean, stdev = rolling_stats(spreads, size)
    z_value = (current - mean) / stdev

    cost = expected_cost(principal, policy.fee_rate, policy.slippage_bps, policy.funding_buffer)
    magnitude = abs(z_value)
    if magnitude >= policy.stop_z:
        return SignalDecision(
            ACTION_STOP, z_value, current, mean, stdev, cost, REASON_STOP_LOSS_TRIGGERED
        )
    if magnitude >= policy.entry_z:
        convergence_gain = abs(current - mean) * principal
        if convergence_gain <= cost:
            return SignalDecision(
                ACTION_NONE,
                z_value,
                current,
                mean,
                stdev,
                cost,
                REASON_COST_FILTER_NOT_PASSED,
            )
        direction = DIRECTION_SHORT_A_LONG_B if z_value > _ZERO else DIRECTION_LONG_A_SHORT_B
        return SignalDecision(
            ACTION_OPEN,
            z_value,
            current,
            mean,
            stdev,
            cost,
            REASON_ENTRY_THRESHOLD_REACHED,
            direction,
        )
    if magnitude <= policy.exit_z:
        return SignalDecision(
            ACTION_CLOSE, z_value, current, mean, stdev, cost, REASON_EXIT_THRESHOLD_REACHED
        )
    return SignalDecision(ACTION_NONE, z_value, current, mean, stdev, cost, REASON_NO_SIGNAL)


__all__ = [
    "ACTION_CLOSE",
    "ACTION_NONE",
    "ACTION_OPEN",
    "ACTION_STOP",
    "DIRECTION_LONG_A_SHORT_B",
    "DIRECTION_SHORT_A_LONG_B",
    "REASON_COST_FILTER_NOT_PASSED",
    "REASON_ENTRY_THRESHOLD_REACHED",
    "REASON_EXIT_THRESHOLD_REACHED",
    "REASON_NO_SIGNAL",
    "REASON_STOP_LOSS_TRIGGERED",
    "PriceSample",
    "SignalDecision",
    "SignalPolicy",
    "SignalUnavailable",
    "evaluate_signal",
    "expected_cost",
    "log_spread_value",
    "rolling_stats",
]
