"""观察缺口记账。

研究侧两个引擎都在 ``apply()`` 里做同一件事：与上一次观察的间隔超过
``max_observation_gap_seconds`` 就记一次 ``observation_gap`` 事件
（``bull-call-forward/forward.py`` 与 ``options-forward/forward.py``）。

产品此前只有 WebSocket 深度缺口，没有**采样级**的观察缺口记账：worker
停机、行情失败或定时器漂移导致的长时间静默，在产品里看不出来。这里把它
收敛成一个纯函数，供采样循环调用。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class ObservationGap:
    """一次观察缺口。"""

    base: str
    previous_ms: int
    observed_ms: int
    gap_seconds: Decimal
    max_gap_seconds: int


def detect_gap(
    *,
    base: str,
    previous_ms: int,
    observed_ms: int,
    max_gap_seconds: int,
) -> ObservationGap | None:
    """判断这次观察是否构成缺口。

    Args:
        base: 标的（``"BTC"`` 等）。
        previous_ms: 上一次观察时刻（毫秒）。
        observed_ms: 本次观察时刻（毫秒）。
        max_gap_seconds: 允许的最大间隔（秒）。

    Returns:
        构成缺口时返回 :class:`ObservationGap`，否则返回 ``None``。
        时间不前进（``observed_ms <= previous_ms``）属于乱序，抛出
        ``ValueError``，不能伪装成连续的正常观察。
    """
    if type(max_gap_seconds) is not int or max_gap_seconds <= 0:
        raise ValueError("最大观察间隔必须是正整数")
    if any(type(value) is not int or value < 0 for value in (previous_ms, observed_ms)):
        raise ValueError("观察时间必须是非负整数毫秒")
    if observed_ms <= previous_ms:
        raise ValueError("观察时间必须严格递增")
    if observed_ms - previous_ms <= max_gap_seconds * 1000:
        return None
    return ObservationGap(
        base=base,
        previous_ms=previous_ms,
        observed_ms=observed_ms,
        gap_seconds=Decimal(observed_ms - previous_ms) / Decimal(1000),
        max_gap_seconds=max_gap_seconds,
    )


__all__ = [
    "ObservationGap",
    "detect_gap",
]
