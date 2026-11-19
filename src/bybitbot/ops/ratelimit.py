"""意图提交速率上限（NFR-01 / AC-22）。

需求原文：
    **NFR-01** 首期容量：1 个账户、最多 5 个策略实例、10 个现货交易对、
    200 个候选期权合约；**策略合计提交意图不超过 1 次/秒**，低于交易所实际限额运行。

这是对交易所限流的保护，不是吞吐目标 —— 因此上限的作用是**限速**，不是**拒单**：
超出上限的计划执行被**延后**到下一个调度周期再提交，而不是丢弃。

为什么不能丢弃：多个策略的日历时刻可能落在同一秒（例如两个策略都在 00:00 UTC）。
若把超出的计划直接跳过，其中一个策略当天就静默不交易了 —— 这是"限额"被
误用成"拒单"。延后既守住了每秒上限，也保住了计划本身；延后有界：下一个
调度周期若已错过宽限期，按"错过即跳过"处理，不集中补做（STR-02）。

跳过/延后的事实都落在 ``schedule_run.result`` 与告警里，不留静默缺口。
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable


class SlidingWindowRateLimiter:
    """滑动窗口速率上限。

    Args:
        limit_per_second: 每秒允许的次数（整数）。
        monotonic: 单调钟来源 —— 速率判定不受墙上时钟跳变影响。
    """

    def __init__(
        self,
        limit_per_second: int = 1,
        *,
        monotonic: Callable[[], float],
    ) -> None:
        if limit_per_second < 1:
            raise ValueError("每秒上限必须至少为 1")
        self._limit = limit_per_second
        self._monotonic = monotonic
        self._events: deque[float] = deque()

    @property
    def limit_per_second(self) -> int:
        return self._limit

    def allow(self) -> bool:
        """是否允许再提交一次意图。

        允许时占用一个额度；不允许时**不**占用，也不排队。
        """
        now = self._monotonic()
        window_start = now - 1.0
        while self._events and self._events[0] <= window_start:
            self._events.popleft()
        if len(self._events) >= self._limit:
            return False
        self._events.append(now)
        return True

    def used(self) -> int:
        """当前窗口内已占用的额度。"""
        now = self._monotonic()
        window_start = now - 1.0
        while self._events and self._events[0] <= window_start:
            self._events.popleft()
        return len(self._events)

    def seconds_until_next_slot(self) -> float:
        """距离下一个可用额度还有多久（秒）。

        窗口已满时，等最老的一次占用滑出窗口即可；窗口未满时为 0。
        """
        now = self._monotonic()
        window_start = now - 1.0
        while self._events and self._events[0] <= window_start:
            self._events.popleft()
        if len(self._events) < self._limit:
            return 0.0
        return max(0.0, self._events[0] + 1.0 - now)


__all__ = ["SlidingWindowRateLimiter"]
