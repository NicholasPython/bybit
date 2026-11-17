"""时钟跳变与交易所偏差监控（NFR-06 / AC-26）。

需求原文：
    **NFR-06** 服务器启用时间同步，监测与交易所时间的偏差；最大允许偏差必须配置
    并与请求有效窗口匹配。超限或时钟跳变时暂停新开仓并告警，不通过无限增大请求
    窗口掩盖问题。超时和退避采用单调时钟；调度使用持久化 UTC 计划标识避免时钟
    回拨重复执行。

本模块只做**检测**，不做任何补偿式时钟调整：

    * :meth:`ClockWatch.observe` 比较墙钟与单调钟的增量，识别跳变 —— 前跳与回拨都算；
    * :meth:`ClockWatch.measure_skew` 用一次服务器时间读取测量本机与交易所的偏差。

两个时间源都可注入，测试因此能确定性地复现跳变与偏差，而不必真的改系统时间。
超时与退避一律走 :mod:`bybitbot.clock` 的单调钟，不受墙上时钟影响。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass

from bybitbot.clock import monotonic, now_utc


@dataclass(frozen=True)
class ClockObservation:
    """一次跳变观测。

    ``jump_seconds`` 是墙钟增量与单调钟增量的差值绝对值；超过阈值即判定跳变。
    ``direction`` 区分前跳（``forward``）与回拨（``backward``）。
    """

    wall: dt.datetime
    monotonic: float
    wall_delta_seconds: float
    monotonic_delta_seconds: float
    jump_seconds: float
    jumped: bool
    direction: str

    @property
    def detail(self) -> str:
        return f"{'前跳' if self.direction == 'forward' else '回拨'} {self.jump_seconds:.1f}s"


@dataclass(frozen=True)
class SkewObservation:
    """一次交易所偏差观测。

    ``offset_seconds`` 为正表示交易所时间领先本机；``round_trip_seconds`` 是
    这次读取的往返耗时，说明测量本身的精度上限。
    """

    offset_seconds: float
    round_trip_seconds: float
    exceeded: bool


class ClockWatch:
    """时钟跳变与交易所偏差监控。

    Args:
        max_skew_seconds: 最大允许偏差；超过即判定不可用，调用方暂停新开仓。
        skew_interval_seconds: 两次偏差测量之间的最小间隔。
        recovery_interval_seconds: 存在未解除的偏差暂停时，复测偏差的间隔 ——
            偏差恢复要尽快确认，但不能每个 tick 都去打扰交易所。
        wall_clock: 墙钟来源（默认 :func:`bybitbot.clock.now_utc`）。
        monotonic_clock: 单调钟来源（默认 :func:`bybitbot.clock.monotonic`）。
    """

    def __init__(
        self,
        *,
        max_skew_seconds: float,
        skew_interval_seconds: float = 60.0,
        recovery_interval_seconds: float = 10.0,
        wall_clock: Callable[[], dt.datetime] = now_utc,
        monotonic_clock: Callable[[], float] = monotonic,
    ) -> None:
        if max_skew_seconds <= 0:
            raise ValueError("最大允许偏差必须为正数")
        self._max_skew = float(max_skew_seconds)
        self._skew_interval = float(skew_interval_seconds)
        self._recovery_interval = float(recovery_interval_seconds)
        self._wall = wall_clock
        self._monotonic = monotonic_clock
        self._last_wall = self._wall()
        self._last_mono = self._monotonic()
        self._skew_checked_at: float | None = None
        self._last_jump: ClockObservation | None = None

    @property
    def max_skew_seconds(self) -> float:
        return self._max_skew

    @property
    def last_jump(self) -> ClockObservation | None:
        """最近一次跳变观测；没有跳变过就是 ``None``。"""
        return self._last_jump

    @property
    def skew_checked(self) -> bool:
        """是否至少测量过一次交易所偏差。"""
        return self._skew_checked_at is not None

    def observe(self) -> ClockObservation:
        """观测一次时钟状态，识别跳变。

        每次调用都会推进基线 —— 跳变只报一次，不会每个 tick 重复告警；
        暂停本身由暂停表负责保持，不靠重复上报维持。
        """
        wall, mono = self._wall(), self._monotonic()
        wall_delta = (wall - self._last_wall).total_seconds()
        mono_delta = mono - self._last_mono
        self._last_wall, self._last_mono = wall, mono
        jump = abs(wall_delta - mono_delta)
        exceeded = jump > self._max_skew
        direction = "none"
        if exceeded:
            direction = "forward" if wall_delta > mono_delta else "backward"
        observation = ClockObservation(
            wall=wall,
            monotonic=mono,
            wall_delta_seconds=wall_delta,
            monotonic_delta_seconds=mono_delta,
            jump_seconds=jump,
            jumped=exceeded,
            direction=direction,
        )
        if exceeded:
            self._last_jump = observation
        return observation

    def skew_due(self, *, recovery_pending: bool = False) -> bool:
        """是否到了测量交易所偏差的时间。

        ``recovery_pending`` 表示仍有未解除的偏差暂停：此时按更短的复测间隔
        确认恢复，避免恢复后长时间空转。
        """
        mono = self._monotonic()
        if self._skew_checked_at is None:
            return True
        elapsed = mono - self._skew_checked_at
        if recovery_pending:
            return elapsed >= self._recovery_interval
        return elapsed >= self._skew_interval

    def mark_skew_checked(self) -> None:
        """记录一次偏差测量已完成。"""
        self._skew_checked_at = self._monotonic()

    def measure_skew(
        self,
        *,
        local_before: dt.datetime,
        local_after: dt.datetime,
        remote: dt.datetime,
    ) -> SkewObservation:
        """测量本机与交易所的偏差。

        用读取前后的本机时间中点作为基准，抵消一半的网络往返延迟。
        """
        if remote.tzinfo is None:
            raise ValueError("交易所时间必须带时区")
        midpoint = local_before + (local_after - local_before) / 2
        offset = (remote - midpoint).total_seconds()
        return SkewObservation(
            offset_seconds=offset,
            round_trip_seconds=(local_after - local_before).total_seconds(),
            exceeded=abs(offset) > self._max_skew,
        )


__all__ = ["ClockObservation", "ClockWatch", "SkewObservation"]
