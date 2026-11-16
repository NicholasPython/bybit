"""两腿价差：结构校验、可执行价格、经济性闸门与候选选择。

这一层对应研究侧的三段逻辑：

* ``research/2026-11-15-bull-call-forward/forward.py`` 的 ``structure()``、
  ``economics()``、``choose()``、``limit_price()``、``executable()``；
* ``research/2026-11-15-options-forward/forward.py`` 的 ``contract_ok()``。

产品此前只有单腿买入，既没有空腿，也没有行权价配对，更没有价差级的成本
闸门。这里把判断口径搬进公共层，全部是**纯函数**：输入腿的行情与规格，
输出可执行价格、经济性结论与候选选择。

术语约定：

* **多头腿**（long leg）—— 买入腿；看涨价差买低行权价 Call，看跌价差买
  高行权价 Put；
* **空头腿**（short leg）—— 卖出腿，行权价顺序与多头腿相反。

费用复用 :func:`bybitbot.terms.option_fee`，步长与取整复用
:mod:`bybitbot.money`，不另写一份算术。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation

from bybitbot.money import common_step, quantize_price
from bybitbot.validation.protocol import SpreadProtocol

_ZERO = Decimal("0")
_ONE = Decimal("1")

#: 研究口径里的可执行状态（不区分大小写）。
TRADING_STATUS = "trading"


@dataclass(frozen=True)
class SpreadLeg:
    """价差的一条腿 —— 一个期权合约的规格与盘口。

    字段名用产品侧的说法（``qty_step``/``tick_size``），但语义与研究侧
    的 ``step``/``tick`` 一一对应。
    """

    symbol: str
    base: str
    status: str
    option_type: str
    expiry_ms: int
    strike: Decimal
    index_price: Decimal
    bid: Decimal
    ask: Decimal
    bid_size: Decimal
    ask_size: Decimal
    tick_size: Decimal
    qty_step: Decimal
    min_qty: Decimal
    max_qty: Decimal
    min_price: Decimal
    max_price: Decimal
    delta: Decimal | None = None
    source_ms: int | None = None
    # 归一化研究行情默认使用 USDT；显式提供的币种仍须校验。
    quote_coin: str = "USDT"
    settle_coin: str = "USDT"

    @property
    def is_tradable(self) -> bool:
        return isinstance(self.status, str) and self.status.lower() == TRADING_STATUS

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> SpreadLeg:
        """从研究侧归一化字典构造（``expiryMs``/``step``/``tick`` 等键名）。

        缺字段或数值非法一律抛 ``ValueError`` —— 拿不到的腿不参与候选。
        """
        expiry = _number(data, "expiryMs", "expiry_ms")
        if expiry != expiry.to_integral_value():
            raise ValueError("到期时刻必须是整数毫秒")
        return cls(
            symbol=_text(data, "symbol"),
            base=_text(data, "base"),
            status=_text(data, "status"),
            option_type=_text(data, "optionType", "optionsType", "option_type", "type", default=""),
            expiry_ms=int(expiry),
            strike=_number(data, "strike"),
            index_price=_number(data, "index", "indexPrice", "index_price"),
            bid=_number(data, "bid", "bidPrice"),
            ask=_number(data, "ask", "askPrice"),
            bid_size=_number(data, "bidSize", "bid_size"),
            ask_size=_number(data, "askSize", "ask_size"),
            tick_size=_number(data, "tick", "tickSize", "tick_size"),
            qty_step=_number(data, "step", "qtyStep", "qty_step"),
            min_qty=_number(data, "minQty", "min_qty"),
            max_qty=_number(data, "maxQty", "max_qty"),
            min_price=_number(data, "minPrice", "min_price"),
            max_price=_number(data, "maxPrice", "max_price"),
            delta=_optional_number(data, "delta"),
            source_ms=_optional_int(data, "sourceMs", "source_ms"),
            quote_coin=_text(data, "quoteCoin", "quote_coin", default="USDT"),
            settle_coin=_text(data, "settleCoin", "settle_coin", default="USDT"),
        )


@dataclass(frozen=True)
class SpreadEconomics:
    """价差级经济性结论。

    ``gates`` 是逐条闸门结果；``eligible`` 为全部通过。
    """

    qty: Decimal
    long_cost: Decimal
    debit: Decimal
    profit: Decimal
    stress_debit: Decimal
    stress_profit: Decimal
    closing_cash: Decimal
    roundtrip: Decimal
    mid: Decimal
    gates: Mapping[str, bool]

    @property
    def eligible(self) -> bool:
        return all(self.gates.values())

    @property
    def failed_gates(self) -> tuple[str, ...]:
        return tuple(name for name, ok in self.gates.items() if not ok)


@dataclass(frozen=True)
class SpreadCandidate:
    """一条被选中的两腿价差候选。"""

    long_leg: SpreadLeg
    short_leg: SpreadLeg
    qty: Decimal
    economics: SpreadEconomics
    reserved_risk: Decimal
    long_limit: Decimal
    short_limit: Decimal


def executable_price(
    leg: SpreadLeg,
    side: str,
    qty: Decimal,
    protocol: SpreadProtocol,
    *,
    entry: bool = False,
    ticks: int | None = None,
) -> Decimal | None:
    """可执行价格（含不利滑点）；不可执行时返回 ``None``。

    Args:
        leg: 腿的行情与规格。
        side: ``"Buy"`` 或 ``"Sell"``。
        qty: 数量。
        protocol: 阈值口径。
        entry: 是否是入场腿 —— 入场腿额外受点差比例闸门约束。
        ticks: 覆盖不利滑点 tick 数（压力测试用）。
    """
    if not leg.is_tradable or not _finite(qty) or qty <= 0:
        return None
    numbers = (
        leg.bid,
        leg.ask,
        leg.bid_size,
        leg.ask_size,
        leg.index_price,
        leg.tick_size,
        leg.qty_step,
        leg.min_qty,
        leg.max_qty,
        leg.min_price,
        leg.max_price,
    )
    if not _finite(*numbers):
        return None
    if not 0 < leg.bid <= leg.ask:
        return None
    if leg.index_price <= 0 or not _lot_ok(leg) or leg.tick_size <= 0:
        return None
    if not 0 <= leg.min_price <= leg.max_price:
        return None
    if not leg.min_qty <= qty <= leg.max_qty:
        return None
    if qty % leg.qty_step != 0:
        return None
    if min(leg.bid_size, leg.ask_size) < qty:
        return None
    if leg.bid % leg.tick_size != 0 or leg.ask % leg.tick_size != 0:
        return None
    if entry:
        mid = (leg.ask + leg.bid) / 2
        if mid <= 0 or (leg.ask - leg.bid) / mid > protocol.max_spread_fraction:
            return None
    slippage = protocol.adverse_slippage_ticks if ticks is None else ticks
    if not isinstance(slippage, int) or isinstance(slippage, bool) or slippage < 0:
        return None
    if side == "Buy":
        price = leg.ask + slippage * leg.tick_size
    elif side == "Sell":
        price = leg.bid - slippage * leg.tick_size
    else:
        return None
    if price <= 0 or not leg.min_price <= price <= leg.max_price:
        return None
    return price


def structure_ok(
    long_leg: SpreadLeg,
    short_leg: SpreadLeg,
    now_ms: int,
    protocol: SpreadProtocol,
    *,
    check_deltas: bool = True,
    direction: str = "Call",
) -> bool:
    """同标的、同到期、USDT 借记价差的入场结构。

    ``direction`` 为 Call 时买低行权价，为 Put 时买高行权价。Delta 须符合
    方向（Call 正、Put 负），其绝对值落在协议带内。成交复核可跳过 Delta。
    """
    if not _pair_ok(long_leg, short_leg, direction):
        return False
    if not isinstance(now_ms, int) or isinstance(now_ms, bool):
        return False
    remaining = long_leg.expiry_ms - now_ms
    low, high = protocol.entry_window_ms
    if not low <= remaining <= high:
        return False
    if not _finite(long_leg.index_price, short_leg.index_price):
        return False
    if min(long_leg.index_price, short_leg.index_price) <= 0:
        return False
    index = (long_leg.index_price + short_leg.index_price) / 2
    width_index = abs(short_leg.strike - long_leg.strike) / index
    if not protocol.width_index_min <= width_index <= protocol.width_index_max:
        return False
    if check_deltas:
        if not _finite(long_leg.delta, short_leg.delta):
            return False
        sign = 1 if direction == "Call" else -1
        if not protocol.long_delta_min <= sign * long_leg.delta <= protocol.long_delta_max:
            return False
        if not protocol.short_delta_min <= sign * short_leg.delta <= protocol.short_delta_max:
            return False
    return True


def spread_economics(
    long_leg: SpreadLeg,
    short_leg: SpreadLeg,
    qty: Decimal,
    protocol: SpreadProtocol,
    *,
    paid_long: Decimal | None = None,
    direction: str = "Call",
) -> SpreadEconomics | None:
    """价差级经济性与闸门结论；结构或行情不成立时返回 ``None``。

    Args:
        long_leg: 多头腿。
        short_leg: 空头腿。
        qty: 两腿共用的数量。
        protocol: 阈值口径。
        paid_long: 多头腿已经付出的含费成本（第二腿成交后复算时传入）。
        direction: Call 买低卖高；Put 买高卖低，不检查入场时间窗或 Delta。
    """
    if not _pair_ok(long_leg, short_leg, direction):
        return None
    if paid_long is not None and (not _finite(paid_long) or paid_long <= 0):
        return None
    sell = executable_price(short_leg, "Sell", qty, protocol, entry=True)
    close_long = executable_price(long_leg, "Sell", qty, protocol)
    close_short = executable_price(short_leg, "Buy", qty, protocol)
    stress_sell = executable_price(
        short_leg, "Sell", qty, protocol, entry=True, ticks=protocol.stress_adverse_ticks
    )
    if any(price is None for price in (sell, close_long, close_short, stress_sell)):
        return None
    if paid_long is None:
        buy = executable_price(long_leg, "Buy", qty, protocol, entry=True)
        stress_buy = executable_price(
            long_leg, "Buy", qty, protocol, entry=True, ticks=protocol.stress_adverse_ticks
        )
        if buy is None or stress_buy is None:
            return None
        long_cost = buy * qty + protocol.option_fee(
            premium=buy, qty=qty, index_price=long_leg.index_price
        )
        stress_cost = stress_buy * qty + protocol.option_fee(
            premium=stress_buy, qty=qty, index_price=long_leg.index_price
        )
    else:
        long_cost = stress_cost = paid_long
    debit = (
        long_cost
        - sell * qty
        + protocol.option_fee(premium=sell, qty=qty, index_price=short_leg.index_price)
    )
    width = abs(short_leg.strike - long_leg.strike) * qty
    profit = width - debit
    stress_debit = (
        stress_cost
        - stress_sell * qty
        + protocol.option_fee(premium=stress_sell, qty=qty, index_price=short_leg.index_price)
    )
    stress_profit = width - stress_debit
    if min(debit, profit, stress_debit, stress_profit) <= 0:
        return None

    closing_cash = (
        (close_long - close_short) * qty
        - protocol.option_fee(premium=close_long, qty=qty, index_price=long_leg.index_price)
        - protocol.option_fee(premium=close_short, qty=qty, index_price=short_leg.index_price)
    )
    roundtrip = debit - closing_cash
    mid = (long_leg.ask + long_leg.bid - short_leg.ask - short_leg.bid) / 2 * qty
    gates = {
        "reward_risk": profit / debit >= protocol.min_reward_risk,
        "take_profit_reachable": profit >= debit * protocol.take_profit_debit_fraction,
        "entry_friction": debit - mid <= profit * protocol.max_entry_friction_profit,
        "roundtrip_risk": roundtrip <= debit * protocol.max_roundtrip_loss_risk,
        "roundtrip_profit": roundtrip <= profit * protocol.max_roundtrip_loss_profit,
        "stress_reward_risk": stress_profit / stress_debit >= protocol.min_reward_risk,
        "first_leg_outlay": long_cost <= protocol.max_first_leg_outlay_usdt,
    }
    return SpreadEconomics(
        qty=qty,
        long_cost=long_cost,
        debit=debit,
        profit=profit,
        stress_debit=stress_debit,
        stress_profit=stress_profit,
        closing_cash=closing_cash,
        roundtrip=roundtrip,
        mid=mid,
        gates=gates,
    )


def choose_candidate(
    *,
    base: str,
    legs: Sequence[SpreadLeg],
    now_ms: int,
    available: Decimal,
    protocol: SpreadProtocol,
    direction: str = "Call",
) -> SpreadCandidate | None:
    """在候选池里选出一条两腿价差；没有合格候选时返回 ``None``。

    排序口径（与研究侧一致）：到期距离目标越近越好，其次到期时刻、空头
    Delta 贴近目标、多头 Delta 贴近目标、行权价、合约代码。

    Args:
        base: 标的。
        legs: 该标的的腿池。
        now_ms: 当前时刻（毫秒）。
        available: 可用的风险预算（USDT）。
        protocol: 阈值口径。
        direction: Call 或 Put；Delta 排序比较绝对值与协议目标。
    """
    if direction not in ("Call", "Put") or not _finite(available) or available <= 0:
        return None
    pool = [leg for leg in legs if leg.base == base and leg.is_tradable and _lot_ok(leg)]
    candidates: list[tuple[tuple, SpreadCandidate]] = []
    sign = 1 if direction == "Call" else -1
    for long_leg in pool:
        if not _finite(long_leg.delta):
            continue
        if not protocol.long_delta_min <= sign * long_leg.delta <= protocol.long_delta_max:
            continue
        for short_leg in pool:
            if short_leg.symbol == long_leg.symbol:
                continue
            if not structure_ok(long_leg, short_leg, now_ms, protocol, direction=direction):
                continue
            step = common_step(long_leg.qty_step, short_leg.qty_step)
            minimum = _ceil_step(max(long_leg.min_qty, short_leg.min_qty), step)
            if minimum <= 0:
                continue
            economics = spread_economics(
                long_leg, short_leg, minimum, protocol, direction=direction
            )
            if economics is None or not economics.eligible:
                continue
            cap = min(
                available / (economics.stress_debit / minimum),
                protocol.max_first_leg_outlay_usdt / (economics.long_cost / minimum),
                long_leg.bid_size,
                long_leg.ask_size,
                short_leg.bid_size,
                short_leg.ask_size,
                long_leg.max_qty,
                short_leg.max_qty,
            )
            qty = _floor_step(cap, step)
            if qty < minimum:
                continue
            economics = spread_economics(long_leg, short_leg, qty, protocol, direction=direction)
            if economics is None or not economics.eligible or economics.stress_debit > available:
                continue
            rank = (
                abs(long_leg.expiry_ms - now_ms - protocol.target_expiry_offset_ms),
                long_leg.expiry_ms,
                abs(abs(short_leg.delta) - protocol.target_short_delta),
                abs(abs(long_leg.delta) - protocol.target_long_delta),
                long_leg.strike,
                short_leg.strike,
                long_leg.symbol,
                short_leg.symbol,
            )
            candidates.append(
                (
                    rank,
                    SpreadCandidate(
                        long_leg=long_leg,
                        short_leg=short_leg,
                        qty=qty,
                        economics=economics,
                        reserved_risk=economics.stress_debit,
                        long_limit=limit_price(long_leg, "Buy", protocol),
                        short_limit=limit_price(short_leg, "Sell", protocol),
                    ),
                )
            )
    if not candidates:
        return None
    return min(candidates, key=lambda item: item[0])[1]


def limit_price(leg: SpreadLeg, side: str, protocol: SpreadProtocol) -> Decimal:
    """入场限价。

    买入按入场容差向上取整到 tick，卖出按入场容差向下取整。
    """
    fraction = protocol.entry_limit_fraction
    if side == "Buy":
        return quantize_price(leg.ask * (_ONE + fraction), leg.tick_size, up=True)
    if side == "Sell":
        return quantize_price(leg.bid * (_ONE - fraction), leg.tick_size, up=False)
    raise ValueError("side 必须是 Buy 或 Sell")


def remaining_risk_budget(
    reserved: Mapping[str, Decimal],
    protocol: SpreadProtocol,
    *,
    excluding: str | None = None,
) -> Decimal:
    """cohort 级剩余风险预算。

    Args:
        reserved: 各标的已预留的风险金额。
        protocol: 阈值口径。
        excluding: 排除的标的（重算该标的自身时用）。
    """
    if any(not _finite(value) or value < 0 for value in reserved.values()):
        raise ValueError("预留风险必须为非负有限 Decimal")
    used = sum((value for name, value in reserved.items() if name != excluding), _ZERO)
    return max(_ZERO, protocol.aggregate_risk_budget_usdt - used)


def bear_contract_ok(
    long_leg: SpreadLeg,
    short_leg: SpreadLeg,
    qty: Decimal,
    protocol: SpreadProtocol,
) -> bool:
    """看跌价差的合约校验。

    两腿都是 Put、同一到期、同一标的，**多头腿行权价更高**（与条件看涨相反），
    数量满足两腿最小量与步长。
    """
    if not _pair_ok(long_leg, short_leg, "Put") or not _finite(qty) or qty <= 0:
        return False
    for leg in (long_leg, short_leg):
        if not leg.is_tradable or not _lot_ok(leg):
            return False
        if not leg.min_qty <= qty <= leg.max_qty or qty % leg.qty_step != 0:
            return False
    return True


def _finite(*values: object) -> bool:
    return all(isinstance(value, Decimal) and value.is_finite() for value in values)


def _lot_ok(leg: SpreadLeg) -> bool:
    return (
        _finite(leg.qty_step, leg.min_qty, leg.max_qty)
        and leg.qty_step > 0
        and 0 < leg.min_qty <= leg.max_qty
    )


def _pair_ok(long_leg: SpreadLeg, short_leg: SpreadLeg, direction: str) -> bool:
    if direction not in ("Call", "Put"):
        return False
    for leg in (long_leg, short_leg):
        if (
            leg.option_type != direction
            or leg.quote_coin != "USDT"
            or leg.settle_coin != "USDT"
            or not isinstance(leg.base, str)
            or not leg.base
            or not isinstance(leg.expiry_ms, int)
            or isinstance(leg.expiry_ms, bool)
            or leg.expiry_ms <= 0
            or not _finite(leg.strike)
            or leg.strike <= 0
        ):
            return False
    if long_leg.base != short_leg.base or long_leg.expiry_ms != short_leg.expiry_ms:
        return False
    if direction == "Call":
        return long_leg.strike < short_leg.strike
    return long_leg.strike > short_leg.strike


def _ceil_step(value: Decimal, step: Decimal) -> Decimal:
    """向上取到步长整数倍。"""
    return (value / step).to_integral_value(rounding=ROUND_CEILING) * step


def _floor_step(value: Decimal, step: Decimal) -> Decimal:
    """向下取到步长整数倍。"""
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def _text(data: Mapping[str, object], *names: str, default: str = "") -> str:
    for name in names:
        if name in data:
            value = data[name]
            return value if isinstance(value, str) else ""
    return default


def _number(data: Mapping[str, object], *names: str) -> Decimal:
    for name in names:
        if name not in data:
            continue
        value = data[name]
        try:
            parsed = value if isinstance(value, Decimal) else Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"字段 {name} 不是合法数值") from exc
        if not parsed.is_finite():
            raise ValueError(f"字段 {name} 必须是有限数值")
        return parsed
    raise ValueError(f"缺少字段 {names[0]}")


def _optional_number(data: Mapping[str, object], *names: str) -> Decimal | None:
    try:
        return _number(data, *names)
    except ValueError:
        return None


def _optional_int(data: Mapping[str, object], *names: str) -> int | None:
    value = _optional_number(data, *names)
    if value is None or value != value.to_integral_value():
        return None
    return int(value)


__all__ = [
    "TRADING_STATUS",
    "SpreadCandidate",
    "SpreadEconomics",
    "SpreadLeg",
    "bear_contract_ok",
    "choose_candidate",
    "executable_price",
    "limit_price",
    "remaining_risk_budget",
    "spread_economics",
    "structure_ok",
]
