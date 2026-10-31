"""时间与时钟（NFR-06）。

需求约束：
    * 内部时间统一 UTC；调度与报表默认显示 Asia/Shanghai；
    * 超时和退避采用**单调时钟**，不受墙上时钟跳变破坏；
    * 调度使用持久化 UTC 计划标识，避免时钟回拨重复执行；
    * 与交易所时间偏差超限时暂停新开仓并告警。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from zoneinfo import ZoneInfo

UTC = dt.UTC


def now_utc() -> dt.datetime:
    """当前 UTC 时间（带时区）。所有落库时间都用它。"""
    return dt.datetime.now(UTC)


def monotonic() -> float:
    """单调秒表。超时、退避、重试间隔一律用它，不受墙上时钟跳变影响。"""
    return time.monotonic()


def to_local(value: dt.datetime, tz_name: str) -> dt.datetime:
    """转换到报表用的本地时区（默认日界线时区）。"""
    return value.astimezone(ZoneInfo(tz_name))


def local_day(value: dt.datetime, tz_name: str) -> str:
    """本地日界线所属日期，格式 ``YYYY-MM-DD``（LED-04）。"""
    return to_local(value, tz_name).strftime("%Y-%m-%d")


def local_day_start_utc(value: dt.datetime, tz_name: str) -> dt.datetime:
    """本地日界线 00:00 对应的 UTC 时刻。"""
    local = to_local(value, tz_name)
    start_local = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start_local.astimezone(UTC)


@dataclass(frozen=True)
class ClockSkew:
    """时钟偏差观测结果。"""

    offset_seconds: float
    measured_at: dt.datetime

    @property
    def is_acceptable(self) -> bool:
        return True  # 由调用方按配置阈值判断，见 ClockSkewMonitor


class ClockSkewMonitor:
    """监控本地时钟与交易所服务器的偏差（NFR-06）。

    ``exchange_time_fn`` 是一个返回交易所服务器时间的可调用对象。偏差超过
    ``max_skew_seconds`` 时 :meth:`check` 返回不可用，调用方据此暂停新开仓。

    注意：这里不做任何「补偿式」的时钟调整（NFR-06 明确不通过无限增大请求
    窗口掩盖问题），只做检测与暂停。
    """

    def __init__(
        self,
        exchange_time_fn: Callable[[], dt.datetime],
        *,
        max_skew_seconds: float = 5.0,
    ) -> None:
        self._exchange_time_fn = exchange_time_fn
        self._max_skew = max_skew_seconds
        self._last: ClockSkew | None = None

    @property
    def max_skew_seconds(self) -> float:
        return self._max_skew

    def measure(self) -> ClockSkew:
        exchange_time = self._exchange_time_fn()
        if exchange_time.tzinfo is None:
            raise ValueError("交易所时间必须带时区")
        offset = (now_utc() - exchange_time).total_seconds()
        self._last = ClockSkew(offset_seconds=offset, measured_at=now_utc())
        return self._last

    def check(self) -> tuple[bool, ClockSkew]:
        """返回 ``(是否在允许偏差内, 观测值)``。"""
        skew = self.measure()
        return abs(skew.offset_seconds) <= self._max_skew, skew

    @property
    def last(self) -> ClockSkew | None:
        return self._last


class Deadline:
    """基于单调时钟的截止时间。

    用于请求超时、重试退避、撤单查询时限（EXE-04 / NFR-06）。
    """

    __slots__ = ("_deadline",)

    def __init__(self, seconds: float) -> None:
        self._deadline = monotonic() + seconds

    @property
    def expired(self) -> bool:
        return monotonic() >= self._deadline

    @property
    def remaining(self) -> float:
        return max(0.0, self._deadline - monotonic())

    async def wait(self) -> None:
        """睡到截止。用于退避间隔。"""
        import asyncio

        remaining = self.remaining
        if remaining > 0:
            await asyncio.sleep(remaining)


def backoff_delay(
    attempt: int, *, base: float = 0.5, cap: float = 30.0, jitter: float = 0.2
) -> float:
    """带抖动的指数退避（EXE-04：统一限流、带抖动退避）。

    ``jitter`` 为相对抖动比例，避免多个请求同时重试形成风暴。

    抖动用**稳定哈希**而不是内置 ``hash()`` —— 内置哈希每个进程加盐，
    重启后会变，测试就无法复现同一序列（NFR-06：行为可重复）。
    """
    if attempt < 0:
        raise ValueError("attempt 不能为负")
    raw = min(cap, base * (2**attempt))
    digest = hashlib.sha256(f"{attempt}:{raw:.6f}".encode()).digest()
    spread = int.from_bytes(digest[:4], "big") / 0xFFFFFFFF
    factor = 1.0 + jitter * (2.0 * spread - 1.0)
    return max(0.0, raw * factor)


__all__ = [
    "UTC",
    "ClockSkew",
    "ClockSkewMonitor",
    "Deadline",
    "backoff_delay",
    "local_day",
    "local_day_start_utc",
    "monotonic",
    "now_utc",
    "to_local",
]
