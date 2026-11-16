"""期权合约条款解析（公共层）。

Bybit 期权合约符号形如::

    BTC-27NOV26-65000-P-USDT
    └base┘ └DDMMMYY┘ └strike┘ └C/P┘ └settle┘

到期时刻固定为**交割日 08:00 UTC**（Bybit 期权统一交割时间）。

这一层由控制台与研究侧共用：两侧对同一个 symbol 的**行权价、到期时刻、
期权类型**必须解释一致。否则费用、权利金预算、到期处理会各自算错，而且
两边的账本无法互相对账。

容错约定
--------
``parse_option_symbol`` 对"不是期权符号"或"格式非法"返回 ``None``，
供需要兼容降级的调用方使用（控制台适配器：symbol 解析失败时退回交易所
返回的 ``strikePrice``）。``require_option_symbol`` 直接抛错，供要求
严格一致的调用方使用（研究侧账本）。
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from decimal import Decimal

#: 交割时刻（UTC 小时）。Bybit 期权在交割日 08:00 UTC 交割。
DELIVERY_HOUR_UTC = 8

_MONTHS = (
    "JAN",
    "FEB",
    "MAR",
    "APR",
    "MAY",
    "JUN",
    "JUL",
    "AUG",
    "SEP",
    "OCT",
    "NOV",
    "DEC",
)

_OPTION_SYMBOL = re.compile(
    r"^(?P<base>[A-Z0-9]{2,12})"
    r"-(?P<day>\d{1,2})(?P<month>[A-Z]{3})(?P<year>\d{2})"
    r"-(?P<strike>[0-9]+(?:\.[0-9]+)?)"
    r"-(?P<option_type>[CP])"
    r"(?:-(?P<settle>[A-Z]{2,12}))?$"
)


@dataclass(frozen=True)
class OptionTerms:
    """期权合约的静态条款。"""

    #: 完整合约符号。
    symbol: str
    #: 标的币种（BTC / ETH …）。
    base: str
    #: ``Call`` 或 ``Put``。
    option_type: str
    #: 行权价。
    strike: Decimal
    #: 到期时刻（UTC，08:00 交割）。
    expiry: dt.datetime
    #: 到期时刻（毫秒时间戳），与交易所 ``deliveryTime`` 同一口径。
    expiry_ms: int
    #: 符号明确写出的结算币；旧式符号不带后缀时为 None。
    settle: str | None = None


def _expiry(day: int, month: str, year: int) -> dt.datetime:
    """由 DDMMMYY 计算到期时刻（08:00 UTC）。"""
    if month not in _MONTHS:
        raise ValueError(f"未知月份：{month}")
    return dt.datetime(
        2000 + year,
        _MONTHS.index(month) + 1,
        day,
        DELIVERY_HOUR_UTC,
        tzinfo=dt.UTC,
    )


def parse_option_symbol(symbol: str) -> OptionTerms | None:
    """解析期权合约符号。

    Args:
        symbol: 合约符号。

    Returns:
        解析结果；不是期权符号或格式非法时返回 ``None``。
    """
    if not isinstance(symbol, str):
        return None
    match = _OPTION_SYMBOL.fullmatch(symbol)
    if match is None:
        return None
    try:
        expiry = _expiry(
            int(match.group("day")),
            match.group("month"),
            int(match.group("year")),
        )
    except ValueError:
        return None
    strike = Decimal(match.group("strike"))
    if strike <= 0:
        return None
    option_type = "Call" if match.group("option_type") == "C" else "Put"
    return OptionTerms(
        symbol=symbol,
        base=match.group("base"),
        option_type=option_type,
        strike=strike,
        expiry=expiry,
        expiry_ms=int(expiry.timestamp()) * 1000,
        settle=match.group("settle"),
    )


def require_option_symbol(symbol: str) -> OptionTerms:
    """解析期权合约符号，非法时抛错。

    研究侧的账本与风控用这个入口：symbol 解释不了就不该继续算钱。
    """
    terms = parse_option_symbol(symbol)
    if terms is None:
        raise ValueError(f"不是合法的期权合约符号：{symbol!r}")
    return terms


def option_symbol(
    *,
    base: str,
    expiry: dt.datetime,
    strike: Decimal,
    option_type: str,
    settle: str = "USDT",
) -> str:
    """由条款拼出合约符号（与 :func:`parse_option_symbol` 互逆）。"""
    if option_type not in {"Call", "Put"}:
        raise ValueError("option_type 只能是 Call 或 Put")
    if not strike.is_finite() or strike <= 0:
        raise ValueError("行权价必须为正的有限数")
    base, settle = base.upper(), settle.upper()
    if not re.fullmatch(r"[A-Z0-9]{2,12}", base) or not re.fullmatch(r"[A-Z]{2,12}", settle):
        raise ValueError("标的或结算币格式非法")
    if expiry.tzinfo is None or expiry.utcoffset() is None:
        raise ValueError("到期时刻必须带时区")
    expiry = expiry.astimezone(dt.UTC)
    if not 2000 <= expiry.year < 2100 or (
        expiry.hour,
        expiry.minute,
        expiry.second,
        expiry.microsecond,
    ) != (DELIVERY_HOUR_UTC, 0, 0, 0):
        raise ValueError("到期时刻必须在 2000—2099 年的交割日 08:00 UTC")
    month = _MONTHS[expiry.month - 1]
    return (
        f"{base.upper()}-{expiry.day:02d}{month}{expiry.year % 100:02d}"
        f"-{format(strike, 'f')}-{option_type[0].upper()}-{settle.upper()}"
    )


__all__ = [
    "DELIVERY_HOUR_UTC",
    "OptionTerms",
    "option_symbol",
    "parse_option_symbol",
    "require_option_symbol",
]
