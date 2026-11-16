"""期权合约规格的提取与标准化（公共层）。

Bybit ``/v5/market/instruments-info`` 返回的期权条目同时带**符号**与
**成交过滤条件**（``priceFilter`` / ``lotSizeFilter``）。控制台和研究侧
都需要这些字段，但容错程度不同：控制台要能容忍缺失字段并降级，研究侧
要求字段齐全且互相印证（缺一个就拒绝该合约）。

因此这一层分两级：

* :func:`option_filters` —— 只做**提取**，不判断合格性，缺失即 ``None``；
* :func:`option_spec` —— 在提取之上做**严格校验**，供要求一致的调用方使用。

数量上限的取值口径
------------------
交易所同时返回 ``maxOrderQty``（总上限）与 ``maxLimitOrderQty``（限价单上限）。
本模块统一取 ``maxLimitOrderQty``，缺失时退回 ``maxOrderQty`` —— 系统首期
只发限价单，限价单上限才是真正的约束。历史研究脚本曾反向取值（先
``maxOrderQty``），迁移到本层时以这里的口径为准。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from bybitbot.terms.symbols import require_option_symbol


def _number(value: Any) -> Decimal | None:
    """把交易所数值转成 Decimal；缺失或非数值返回 ``None``。

    非有限值（NaN / Infinity）同样视为缺失 —— 交易所不会返回这种值，
    出现即视为数据不可信。
    """
    if value is None or value == "":
        return None
    try:
        parsed = Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


@dataclass(frozen=True)
class OptionFilters:
    """期权成交过滤条件的原始数值（不做合格性判断）。"""

    #: 价格最小变动单位。
    tick: Decimal | None = None
    #: 价格下限。
    min_price: Decimal | None = None
    #: 价格上限。
    max_price: Decimal | None = None
    #: 最小下单数量。
    min_qty: Decimal | None = None
    #: 数量步长。
    step: Decimal | None = None
    #: 最大下单数量（限价单口径）。
    max_qty: Decimal | None = None
    #: 最小下单金额。
    min_order_amt: Decimal | None = None
    #: 最大下单金额。
    max_order_amt: Decimal | None = None


def option_filters(
    price_filter: Mapping[str, Any] | None,
    lot_size_filter: Mapping[str, Any] | None,
) -> OptionFilters:
    """从 ``priceFilter`` / ``lotSizeFilter`` 提取数值。

    缺失或格式错误的字段返回 ``None``，由调用方决定是拒绝还是降级。
    """
    price = price_filter if isinstance(price_filter, Mapping) else {}
    lot = lot_size_filter if isinstance(lot_size_filter, Mapping) else {}
    max_qty = _number(lot.get("maxLimitOrderQty"))
    if max_qty is None:
        max_qty = _number(lot.get("maxOrderQty"))
    return OptionFilters(
        tick=_number(price.get("tickSize")),
        min_price=_number(price.get("minPrice")),
        max_price=_number(price.get("maxPrice")),
        min_qty=_number(lot.get("minOrderQty")),
        step=_number(lot.get("qtyStep")),
        max_qty=max_qty,
        min_order_amt=_number(lot.get("minOrderAmt")),
        max_order_amt=_number(lot.get("maxOrderAmt")),
    )


@dataclass(frozen=True)
class OptionSpec:
    """通过校验的期权合约规格。"""

    symbol: str
    base: str
    option_type: str
    status: str
    expiry_ms: int
    strike: Decimal
    tick: Decimal
    min_price: Decimal
    max_price: Decimal
    min_qty: Decimal
    step: Decimal
    max_qty: Decimal
    min_order_amt: Decimal | None = None
    max_order_amt: Decimal | None = None


def option_spec(
    payload: Mapping[str, Any],
    *,
    base: str,
    option_type: str = "Call",
    quote_coin: str | None = "USDT",
    settle_coin: str | None = "USDT",
) -> OptionSpec:
    """严格校验并标准化一份期权合约条目。

    校验包含符号与元数据的**互相印证**：symbol 推出的到期时刻必须等于
    ``deliveryTime``，symbol 推出的行权价必须等于 ``strikePrice``。不一致
    说明数据源或解析出了问题，此时拒绝该合约而不是猜一个值。

    Raises:
        ValueError: 条目不是目标合约，或字段缺失、越界、互相矛盾。
    """
    if not isinstance(payload, Mapping):
        raise ValueError("合约条目必须是对象")
    symbol = payload.get("symbol")
    if not isinstance(symbol, str) or not symbol:
        raise ValueError("合约条目缺少 symbol")
    if payload.get("optionsType") != option_type:
        raise ValueError(f"合约类型不是 {option_type}")
    if payload.get("baseCoin") != base:
        raise ValueError(f"标的不属于 {base}")
    if quote_coin is not None and payload.get("quoteCoin") != quote_coin:
        raise ValueError(f"计价币不是 {quote_coin}")
    if settle_coin is not None and payload.get("settleCoin") != settle_coin:
        raise ValueError(f"结算币不是 {settle_coin}")

    terms = require_option_symbol(symbol)
    if terms.base != base:
        raise ValueError("symbol 里的标的与 base 不一致")
    if terms.option_type != option_type:
        raise ValueError("symbol 里的类型与 optionsType 不一致")
    if terms.settle is not None and terms.settle != payload.get("settleCoin"):
        raise ValueError("symbol 里的结算币与 settleCoin 不一致")

    delivery = payload.get("deliveryTime")
    delivery_ms = _number(delivery)
    if delivery_ms is None or delivery_ms != delivery_ms.to_integral_value():
        raise ValueError("deliveryTime 不是整数毫秒")
    if int(delivery_ms) != terms.expiry_ms:
        raise ValueError("symbol 的到期时刻与 deliveryTime 不一致")

    if "strikePrice" in payload:
        strike = _number(payload["strikePrice"])
        if strike is None or strike != terms.strike:
            raise ValueError("symbol 的行权价与 strikePrice 不一致")

    status = payload.get("status")
    if not isinstance(status, str) or not status:
        raise ValueError("合约状态缺失")

    filters = option_filters(
        payload.get("priceFilter"),
        payload.get("lotSizeFilter"),
    )
    required = {
        "tickSize": filters.tick,
        "minPrice": filters.min_price,
        "maxPrice": filters.max_price,
        "minOrderQty": filters.min_qty,
        "qtyStep": filters.step,
        "maxOrderQty": filters.max_qty,
    }
    missing = [name for name, value in required.items() if value is None]
    if missing:
        raise ValueError(f"合约过滤条件缺失：{', '.join(sorted(missing))}")
    assert filters.tick is not None
    assert filters.min_price is not None
    assert filters.max_price is not None
    assert filters.min_qty is not None
    assert filters.step is not None
    assert filters.max_qty is not None

    if min(filters.tick, filters.min_qty, filters.step, filters.max_qty) <= 0:
        raise ValueError("合约过滤条件必须为正")
    if filters.max_price <= 0:
        raise ValueError("价格上限必须为正")
    if not 0 <= filters.min_price <= filters.max_price:
        raise ValueError("价格区间不合法")
    if filters.min_qty > filters.max_qty:
        raise ValueError("数量区间不合法")

    return OptionSpec(
        symbol=symbol,
        base=base,
        option_type=option_type,
        status=status,
        expiry_ms=terms.expiry_ms,
        strike=terms.strike,
        tick=filters.tick,
        min_price=filters.min_price,
        max_price=filters.max_price,
        min_qty=filters.min_qty,
        step=filters.step,
        max_qty=filters.max_qty,
        min_order_amt=filters.min_order_amt,
        max_order_amt=filters.max_order_amt,
    )


__all__ = [
    "OptionFilters",
    "OptionSpec",
    "option_filters",
    "option_spec",
]
