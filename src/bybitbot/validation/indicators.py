"""指标与信号（纯函数层）。

研究侧把"行情 → 信号"写在各自的引擎里：

* 条件看涨：``research/2026-11-15-bull-call-forward/market.py`` 的 ``_signal()``
  —— 日线收盘 > SMA20、SMA20 不下降、最近 3 根小时收盘都在 SMA20 与 SMA50
  之上、24 小时收益为正；
* 看跌价差：``research/2026-11-15-options-forward/forward.py`` —— 最新完整小时
  收盘低于日线 SMA20 与 SMA50，且低于 24 小时前。

这里把两套口径收敛成同一组纯函数，产品内的策略与验证任务共用；
输入是**完整** K 线（见 :func:`bybitbot.marketdata.snapshots.completed_closes`），
输出是结构化信号对象。

历史不足时返回 ``eligible=False`` 并给出原因，而不是抛异常 —— 采样循环
需要把"历史还没攒够"记成一种数据状态，而不是让整个采样周期失败。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from bybitbot.validation.protocol import DAY_MS, HOUR_MS

_ZERO = Decimal("0")

#: 24 小时收益与 SMA50 需要的最小样本量。
HOURLY_MINIMUM = 25
DAILY_MINIMUM_STRONG_UP = 51
DAILY_MINIMUM_WEAK_DOWN = 50


class SignalPolicy(StrEnum):
    """信号口径。"""

    #: 条件看涨：转强。
    STRONG_UP = "strong_up"
    #: 看跌价差：走弱。
    WEAK_DOWN = "weak_down"


@dataclass(frozen=True)
class Signal:
    """一次信号判定结果。

    ``eligible`` 才是可用的结论；其余字段是判定依据，会随信号一起留痕，
    便于事后复算"当时为什么这么判"。
    """

    policy: SignalPolicy
    eligible: bool
    reason: str
    hour_close: Decimal | None = None
    daily_close: Decimal | None = None
    sma20: Decimal | None = None
    previous_sma20: Decimal | None = None
    sma50: Decimal | None = None
    last3_hourly_closes: tuple[Decimal, ...] = ()
    return_24h: Decimal | None = None
    last_hour_ms: int | None = None
    last_day_ms: int | None = None


def sma(closes: Sequence[Decimal], window: int) -> Decimal:
    """简单移动平均（最近 ``window`` 个收盘价）。

    Args:
        closes: 收盘价序列，按时间升序。
        window: 窗口长度。

    Raises:
        ValueError: 窗口非正整数、历史长度不足或收盘价无效。
    """
    if isinstance(window, bool) or not isinstance(window, int) or window <= 0:
        raise ValueError("窗口必须为正整数")
    if len(closes) < window:
        raise ValueError("历史长度不足")
    for close in closes:
        _validate_close(close)
    return sum(closes[-window:], _ZERO) / window


def _validate_close(close: Decimal) -> None:
    if not isinstance(close, Decimal) or not close.is_finite() or close <= 0:
        raise ValueError("收盘价必须为有限正 Decimal")


def _history_closes(history: Sequence[tuple[int, Decimal]], width: int) -> list[Decimal]:
    if not isinstance(history, Sequence) or isinstance(history, (str, bytes)):
        raise ValueError("K 线历史必须为序列")
    closes = []
    previous = None
    for row in history:
        if not isinstance(row, (tuple, list)) or len(row) != 2:
            raise ValueError("K 线必须为 (起始毫秒时间戳, 收盘价)")
        timestamp, close = row
        if (
            isinstance(timestamp, bool)
            or not isinstance(timestamp, int)
            or timestamp < 0
            or timestamp % width
        ):
            raise ValueError("K 线起始时间必须为对齐的非负整数毫秒")
        if previous is not None and timestamp - previous != width:
            raise ValueError("K 线历史必须连续且严格递增")
        _validate_close(close)
        closes.append(close)
        previous = timestamp
    return closes


def evaluate_signal(
    *,
    hourly: Sequence[tuple[int, Decimal]],
    daily: Sequence[tuple[int, Decimal]],
    policy: SignalPolicy | str,
) -> Signal:
    """按给定口径判定信号。

    Args:
        hourly: 完整小时 K 线，元素为 ``(起始毫秒时间戳, 收盘价)``。
        daily: 完整日 K 线，同样使用起始时间，历史须连续、对齐且递增。
        policy: 信号枚举或对应字符串值。

    Returns:
        :class:`Signal`。历史不足时 ``eligible`` 为 ``False``；时间字段为收盘边界。

    Raises:
        ValueError: 未知口径、无效收盘价或损坏的历史。调用方负责排除未完成 K 线。
    """
    try:
        policy = SignalPolicy(policy)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"unsupported signal policy: {policy!r}") from exc
    hours = _history_closes(hourly, HOUR_MS)
    days = _history_closes(daily, DAY_MS)
    minimum_daily = (
        DAILY_MINIMUM_STRONG_UP if policy is SignalPolicy.STRONG_UP else DAILY_MINIMUM_WEAK_DOWN
    )
    if len(hours) < HOURLY_MINIMUM:
        return Signal(policy=policy, eligible=False, reason="insufficient_hourly_history")
    if len(days) < minimum_daily:
        return Signal(policy=policy, eligible=False, reason="insufficient_daily_history")

    reference = hours[-HOURLY_MINIMUM]

    sma20 = sma(days, 20)
    sma50 = sma(days, 50)
    hour_close = hours[-1]
    daily_close = days[-1]
    return_24h = hour_close / reference - 1

    common = {
        "hour_close": hour_close,
        "daily_close": daily_close,
        "sma20": sma20,
        "sma50": sma50,
        "return_24h": return_24h,
        "last_hour_ms": hourly[-1][0] + HOUR_MS,
        "last_day_ms": daily[-1][0] + DAY_MS,
    }
    if policy is SignalPolicy.STRONG_UP:
        previous = sma(days[-21:-1], 20)
        last3 = tuple(hours[-3:])
        eligible = (
            daily_close > sma20
            and sma20 >= previous
            and all(close > sma20 and close > sma50 for close in last3)
            and return_24h > 0
        )
        return Signal(
            policy=policy,
            eligible=eligible,
            reason="strong_up" if eligible else "trend_not_confirmed",
            previous_sma20=previous,
            last3_hourly_closes=last3,
            **common,  # type: ignore[arg-type]
        )
    eligible = hour_close < sma20 and hour_close < sma50 and hour_close < reference
    return Signal(
        policy=policy,
        eligible=eligible,
        reason="weak_down" if eligible else "trend_not_confirmed",
        **common,  # type: ignore[arg-type]
    )


__all__ = [
    "DAILY_MINIMUM_STRONG_UP",
    "DAILY_MINIMUM_WEAK_DOWN",
    "HOURLY_MINIMUM",
    "Signal",
    "SignalPolicy",
    "evaluate_signal",
    "sma",
]
