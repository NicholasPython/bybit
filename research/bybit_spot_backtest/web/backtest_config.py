"""Shared calendar window and resource bounds for multi-year backtests."""

import calendar
import datetime as dt

MAX_BACKTEST_YEARS = 5
MAX_BACKTEST_MINUTES = 1827 * 1440  # Upper bound including two leap days.
BACKTEST_TIMEOUT_SECONDS = 600
BACKTEST_CPU_SECONDS = 540


def validate_backtest_window(start, end):
    if start % 60_000 or end % 60_000 or end - start < 30 * 60_000:
        raise ValueError("回测区间须至少 30 分钟，并按整分钟对齐")
    opened = dt.datetime.fromtimestamp(start / 1000, dt.UTC)
    year = opened.year + MAX_BACKTEST_YEARS
    day = min(opened.day, calendar.monthrange(year, opened.month)[1])
    maximum = opened.replace(year=year, day=day)
    if end > int(maximum.timestamp() * 1000):
        raise ValueError("时间范围最多 5 年（按日历年计算）")
