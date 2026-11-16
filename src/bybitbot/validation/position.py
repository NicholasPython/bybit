"""per-base 阶段状态机与现金/费用累计。

对应研究侧 ``research/2026-11-15-bull-call-forward/forward.py`` 的
``new_state()``、``fill()``、``step_position()``，以及
``research/2026-11-15-options-forward/forward.py`` 的同构实现。

产品此前只有"持仓段 + 成交级账本"，没有"每个标的走到哪一步"的中间层，
也没有按标的累计的现金与费用。这里补上：

* :class:`StudyPhase` —— per-base 阶段枚举与合法迁移表；
* :class:`PendingIntent` —— 待成交意图，带决策盘口时间戳与截止时间；
* :class:`PositionLedger` —— 逐标的账本（现金、费用、预留风险、两腿持仓）；
* :class:`FillRecord` —— 成交留痕，含决策到成交的盘口时距。

成交口径沿用研究规则：**只有严格更新的独立盘口才允许成交**（研究侧是
``sourceMs > decisionSourceMs``），否则这仍是上一轮看到的价格。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from bybitbot.validation.protocol import HOUR_MS, SpreadProtocol

_ZERO = Decimal("0")


class StudyPhase(StrEnum):
    """单个标的（base）的阶段。"""

    #: 等待信号与候选。
    WAITING = "waiting"
    #: 多头腿已发出意图、等待成交。
    OPENING_LONG = "opening_long"
    #: 空头腿已发出意图、等待成交。
    OPENING_SHORT = "opening_short"
    #: 两腿都在手。
    OPEN = "open"
    #: 平空头腿中。
    CLOSING_SHORT = "closing_short"
    #: 平多头腿中。
    CLOSING_LONG = "closing_long"
    #: 正常平仓完成。
    CLOSED = "closed"
    #: 入场失败、正在回补已成交的一腿。
    ABORTING = "aborting"
    #: 回补完成、放弃入场。
    ABORTED = "aborted"
    #: 未入场即结束（无持仓）。
    CANCELLED = "cancelled"
    #: 到期时仍有持仓，未解决。
    EXPIRED_UNRESOLVED = "expired_unresolved"
    #: 研究结束时仍有持仓，未解决。
    STUDY_ENDED_UNRESOLVED = "study_ended_unresolved"


#: 终态：不会再迁移。
TERMINAL_PHASES: frozenset[StudyPhase] = frozenset(
    {
        StudyPhase.CLOSED,
        StudyPhase.ABORTED,
        StudyPhase.CANCELLED,
        StudyPhase.EXPIRED_UNRESOLVED,
        StudyPhase.STUDY_ENDED_UNRESOLVED,
    }
)

#: 合法迁移表。
TRANSITIONS: Mapping[StudyPhase, frozenset[StudyPhase]] = {
    StudyPhase.WAITING: frozenset(
        {StudyPhase.OPENING_LONG, StudyPhase.CANCELLED, StudyPhase.EXPIRED_UNRESOLVED}
    ),
    StudyPhase.OPENING_LONG: frozenset(
        {StudyPhase.OPENING_SHORT, StudyPhase.CANCELLED, StudyPhase.EXPIRED_UNRESOLVED}
    ),
    StudyPhase.OPENING_SHORT: frozenset(
        {
            StudyPhase.OPEN,
            StudyPhase.ABORTING,
            StudyPhase.CANCELLED,
            StudyPhase.EXPIRED_UNRESOLVED,
            StudyPhase.STUDY_ENDED_UNRESOLVED,
        }
    ),
    StudyPhase.OPEN: frozenset(
        {
            StudyPhase.CLOSING_SHORT,
            StudyPhase.CANCELLED,
            StudyPhase.EXPIRED_UNRESOLVED,
            StudyPhase.STUDY_ENDED_UNRESOLVED,
        }
    ),
    StudyPhase.CLOSING_SHORT: frozenset(
        {
            StudyPhase.CLOSING_LONG,
            StudyPhase.CANCELLED,
            StudyPhase.EXPIRED_UNRESOLVED,
            StudyPhase.STUDY_ENDED_UNRESOLVED,
        }
    ),
    StudyPhase.CLOSING_LONG: frozenset(
        {
            StudyPhase.CLOSED,
            StudyPhase.CANCELLED,
            StudyPhase.EXPIRED_UNRESOLVED,
            StudyPhase.STUDY_ENDED_UNRESOLVED,
        }
    ),
    StudyPhase.ABORTING: frozenset(
        {StudyPhase.ABORTED, StudyPhase.EXPIRED_UNRESOLVED, StudyPhase.STUDY_ENDED_UNRESOLVED}
    ),
    StudyPhase.CLOSED: frozenset(),
    StudyPhase.ABORTED: frozenset(),
    StudyPhase.CANCELLED: frozenset(),
    StudyPhase.EXPIRED_UNRESOLVED: frozenset(),
    StudyPhase.STUDY_ENDED_UNRESOLVED: frozenset(),
}


class IntentPurpose(StrEnum):
    """一笔待成交意图的用途。"""

    OPEN_LONG = "open_long"
    OPEN_SHORT = "open_short"
    CLOSE_SHORT = "close_short"
    CLOSE_LONG = "close_long"


#: 入场类用途（带截止时间；超时放弃入场）。
OPENING_PURPOSES: frozenset[IntentPurpose] = frozenset(
    {IntentPurpose.OPEN_LONG, IntentPurpose.OPEN_SHORT}
)


@dataclass(frozen=True)
class PendingIntent:
    """一笔待成交意图。

    ``decision_source_ms`` 是**决策时看到的盘口时间戳** —— 之后只有严格更新
    的盘口才允许成交；``deadline_ms`` 只对入场类意图有效。
    """

    purpose: IntentPurpose
    symbol: str
    side: str
    created_ms: int
    decision_source_ms: int
    deadline_ms: int | None = None
    limit: Decimal | None = None

    def __post_init__(self) -> None:
        purpose = IntentPurpose(self.purpose)
        object.__setattr__(self, "purpose", purpose)
        expected = (
            "Buy"
            if purpose
            in {
                IntentPurpose.OPEN_LONG,
                IntentPurpose.CLOSE_SHORT,
            }
            else "Sell"
        )
        if not self.symbol or self.side != expected:
            raise ValueError("意图合约或方向不符")
        times = (self.created_ms, self.decision_source_ms)
        if any(type(value) is not int or value < 0 for value in times):
            raise ValueError("意图时间必须为非负整数毫秒")
        if self.decision_source_ms > self.created_ms:
            raise ValueError("决策盘口时间不能晚于意图创建时间")
        if self.deadline_ms is not None and (
            type(self.deadline_ms) is not int or self.deadline_ms <= self.created_ms
        ):
            raise ValueError("截止时间必须晚于意图创建时间")
        if self.limit is not None and (not self.limit.is_finite() or self.limit <= 0):
            raise ValueError("意图限价必须为正有限数")

    @property
    def is_opening(self) -> bool:
        return self.purpose in OPENING_PURPOSES

    def expired(self, now_ms: int) -> bool:
        """入场意图是否已过截止时间。"""
        return self.deadline_ms is not None and now_ms >= self.deadline_ms

    def accepts_price(self, price: Decimal) -> bool:
        """限价是否被接受（买不高于限价、卖不低于限价）。"""
        if self.limit is None:
            return True
        if self.side == "Buy":
            return price <= self.limit
        if self.side == "Sell":
            return price >= self.limit
        return False


@dataclass(frozen=True)
class FillRecord:
    """一笔成交留痕。

    ``latency_seconds`` 是决策盘口到成交盘口的时距（秒），用于事后核对
    "当时看到的价格与成交价格之间隔了多久"。
    """

    purpose: IntentPurpose
    symbol: str
    side: str
    price: Decimal
    qty: Decimal
    fee: Decimal
    cash_flow: Decimal
    decision_source_ms: int
    quote_source_ms: int
    latency_seconds: Decimal


@dataclass(frozen=True)
class ExitQuote:
    """平仓估值用的可执行价。"""

    price: Decimal
    index_price: Decimal | None = None


@dataclass
class PositionLedger:
    """单个标的（base）的两腿价差账本。

    现金与费用按标的累计：``cash`` 是该标的价差的净现金流（买入为负、
    卖出为正，已扣费用），``fees`` 是累计费用。
    """

    base: str
    phase: StudyPhase = StudyPhase.WAITING
    quantity: Decimal = _ZERO
    cash: Decimal = _ZERO
    fees: Decimal = _ZERO
    reserved_risk: Decimal = _ZERO
    long_symbol: str | None = None
    short_symbol: str | None = None
    long_held: bool = False
    short_held: bool = False
    entry_debit: Decimal | None = None
    opened_ms: int | None = None
    closed_ms: int | None = None
    exit_reason: str | None = None
    pending: PendingIntent | None = None
    events: list[dict[str, object]] = field(default_factory=list)

    # ---- 状态 ----

    @property
    def holding(self) -> bool:
        """是否还持有任意一条腿。"""
        return self.long_held or self.short_held

    @property
    def is_terminal(self) -> bool:
        return self.phase in TERMINAL_PHASES

    def transition(self, target: StudyPhase) -> None:
        """迁移阶段；有持仓时不能把阶段标成已平仓或已取消。"""
        target = StudyPhase(target)
        if target not in TRANSITIONS[self.phase]:
            raise ValueError(f"非法阶段迁移：{self.phase} → {target}")
        if target in {StudyPhase.CLOSED, StudyPhase.ABORTED, StudyPhase.CANCELLED} and self.holding:
            raise ValueError("仍有持仓，不能进入平仓终态")
        self.phase = target
        if self.is_terminal:
            self.pending = None
            if not self.holding:
                self.reserved_risk = _ZERO

    def set_pending(self, intent: PendingIntent) -> None:
        """登记下一腿；已有意图必须先确认成交或取消，不能覆盖。"""
        if self.pending is not None or self.is_terminal:
            raise ValueError("已有待成交意图或账本已结束")
        if not self.quantity.is_finite() or self.quantity <= 0:
            raise ValueError("数量必须为正有限数")
        purpose = IntentPurpose(intent.purpose)
        expected_side = {
            IntentPurpose.OPEN_LONG: "Buy",
            IntentPurpose.OPEN_SHORT: "Sell",
            IntentPurpose.CLOSE_SHORT: "Buy",
            IntentPurpose.CLOSE_LONG: "Sell",
        }[purpose]
        expected_phases = {
            IntentPurpose.OPEN_LONG: {StudyPhase.WAITING, StudyPhase.OPENING_LONG},
            IntentPurpose.OPEN_SHORT: {StudyPhase.OPENING_SHORT},
            IntentPurpose.CLOSE_SHORT: {StudyPhase.OPEN, StudyPhase.CLOSING_SHORT},
            IntentPurpose.CLOSE_LONG: {StudyPhase.CLOSING_LONG, StudyPhase.ABORTING},
        }[purpose]
        if intent.side != expected_side or self.phase not in expected_phases:
            raise ValueError("意图方向或阶段不符")
        is_long = purpose in {IntentPurpose.OPEN_LONG, IntentPurpose.CLOSE_LONG}
        symbol = self.long_symbol if is_long else self.short_symbol
        if symbol is not None and symbol != intent.symbol:
            raise ValueError("意图合约与持仓腿不符")
        if not intent.symbol:
            raise ValueError("意图合约不能为空")
        if purpose is IntentPurpose.OPEN_LONG and self.holding:
            raise ValueError("已有持仓，不能再次开多头腿")
        if purpose is IntentPurpose.OPEN_SHORT and (not self.long_held or self.short_held):
            raise ValueError("开空头腿前必须持有多头腿")
        if purpose is IntentPurpose.CLOSE_SHORT and not self.short_held:
            raise ValueError("没有可平空头腿")
        if purpose is IntentPurpose.CLOSE_LONG and (not self.long_held or self.short_held):
            raise ValueError("平多头腿前必须先平空头腿")
        if purpose is IntentPurpose.OPEN_LONG:
            self.long_symbol = intent.symbol
            if self.phase is StudyPhase.WAITING:
                self.transition(StudyPhase.OPENING_LONG)
        elif purpose is IntentPurpose.OPEN_SHORT:
            if intent.symbol == self.long_symbol:
                raise ValueError("两腿不能使用同一合约")
            self.short_symbol = intent.symbol
        elif purpose is IntentPurpose.CLOSE_SHORT and self.phase is StudyPhase.OPEN:
            self.transition(StudyPhase.CLOSING_SHORT)
        self.pending = intent

    def is_fresh(self, quote_source_ms: int) -> bool:
        """盘口是否严格新于决策时看到的那一份。"""
        return self.pending is not None and quote_source_ms > self.pending.decision_source_ms

    # ---- 成交 ----

    def apply_fill(
        self,
        *,
        purpose: IntentPurpose,
        symbol: str,
        side: str,
        price: Decimal,
        qty: Decimal,
        fee: Decimal,
        quote_source_ms: int,
        now_ms: int,
    ) -> FillRecord:
        """记账一笔成交并推进阶段。

        Raises:
            ValueError: 没有待成交意图、意图不匹配、盘口不够新或数量不符。
        """
        pending = self.pending
        if pending is None:
            raise ValueError("没有待成交意图")
        if pending.purpose != purpose or pending.symbol != symbol or pending.side != side:
            raise ValueError("成交与待成交意图不匹配")
        if any(type(value) is not int or value < 0 for value in (quote_source_ms, now_ms)):
            raise ValueError("成交时间必须为非负整数毫秒")
        if quote_source_ms <= pending.decision_source_ms:
            raise ValueError("盘口没有更新，拒绝成交")
        purpose = IntentPurpose(purpose)
        for value in (price, qty, fee):
            if not isinstance(value, Decimal) or not value.is_finite():
                raise ValueError("成交价格、数量和费用必须为有限数")
        if price <= 0 or qty <= 0 or fee < 0 or qty != self.quantity:
            raise ValueError("成交价格、数量或费用不符")
        if now_ms < pending.created_ms or quote_source_ms > now_ms:
            raise ValueError("成交时间顺序不符")
        if not pending.accepts_price(price) or pending.expired(now_ms):
            raise ValueError("模拟成交超过限价或入场截止时间")
        target = {
            IntentPurpose.OPEN_LONG: StudyPhase.OPENING_SHORT,
            IntentPurpose.OPEN_SHORT: StudyPhase.OPEN,
            IntentPurpose.CLOSE_SHORT: StudyPhase.CLOSING_LONG,
            IntentPurpose.CLOSE_LONG: (
                StudyPhase.ABORTED if self.phase is StudyPhase.ABORTING else StudyPhase.CLOSED
            ),
        }[purpose]
        expected_phases = {
            IntentPurpose.OPEN_LONG: {StudyPhase.OPENING_LONG},
            IntentPurpose.OPEN_SHORT: {StudyPhase.OPENING_SHORT},
            IntentPurpose.CLOSE_SHORT: {StudyPhase.CLOSING_SHORT},
            IntentPurpose.CLOSE_LONG: {StudyPhase.CLOSING_LONG, StudyPhase.ABORTING},
        }[purpose]
        if self.phase not in expected_phases or target not in TRANSITIONS[self.phase]:
            raise ValueError("成交与当前阶段不符")
        flow = (-1 if side == "Buy" else 1) * price * qty - fee
        record = FillRecord(
            purpose=purpose,
            symbol=symbol,
            side=side,
            price=price,
            qty=qty,
            fee=fee,
            cash_flow=flow,
            decision_source_ms=pending.decision_source_ms,
            quote_source_ms=quote_source_ms,
            latency_seconds=Decimal(quote_source_ms - pending.decision_source_ms) / Decimal(1000),
        )
        next_long = self.long_held
        next_short = self.short_held
        if purpose is IntentPurpose.OPEN_LONG:
            next_long = True
        elif purpose is IntentPurpose.OPEN_SHORT:
            next_short = True
        elif purpose is IntentPurpose.CLOSE_SHORT:
            next_short = False
        else:
            next_long = False
        if target in {StudyPhase.CLOSED, StudyPhase.ABORTED} and (next_long or next_short):
            raise ValueError("成交后仍有持仓，不能进入平仓终态")
        next_cash, next_fees = self.cash + flow, self.fees + fee
        # 所有拒绝条件与算术先完成，避免失败后留下半笔现金/费用。
        self.cash, self.fees = next_cash, next_fees
        self.long_held, self.short_held = next_long, next_short
        self.phase = target
        if purpose is IntentPurpose.OPEN_SHORT:
            self.opened_ms = now_ms
            self.entry_debit = -next_cash
            self.reserved_risk = self.entry_debit
        if target in {StudyPhase.CLOSED, StudyPhase.ABORTED}:
            self.closed_ms = now_ms
            self.reserved_risk = _ZERO
        self.pending = None
        self.events.append(
            {
                "kind": "fill",
                "occurred_ms": now_ms,
                "decision_source_ms": pending.decision_source_ms,
                "quote_source_ms": quote_source_ms,
                "purpose": purpose.value,
                "symbol": symbol,
                "side": side,
                "price": str(price),
                "qty": str(qty),
                "fee": str(fee),
                "cash_flow": str(flow),
                "latency_seconds": str(record.latency_seconds),
                "phase": self.phase.value,
            }
        )
        return record

    def abort_entry(self, reason: str) -> StudyPhase:
        """中断入场；先确认原订单撤销，再由调用方登记平多头腿意图。"""
        if self.phase not in {StudyPhase.OPENING_LONG, StudyPhase.OPENING_SHORT}:
            raise ValueError("当前阶段不能中断入场")
        if self.short_held:
            raise ValueError("已有空头腿，必须按正常顺序退出")
        target = StudyPhase.ABORTING if self.long_held else StudyPhase.CANCELLED
        self.transition(target)
        self.pending = None
        self.exit_reason = reason
        return target

    def exit_trigger(
        self,
        *,
        now_ms: int,
        end_ms: int,
        expiry_ms: int,
        signal_eligible: bool,
        net_pnl: Decimal | None,
        protocol: SpreadProtocol,
    ) -> str | None:
        """按研究优先级判定退出原因；缺平仓价仍可触发时间/信号退出。"""
        if self.phase is not StudyPhase.OPEN:
            return None
        if self.opened_ms is None or self.entry_debit is None or self.entry_debit <= 0:
            raise ValueError("完整价差缺少开仓时刻或有效入场借记")
        if now_ms >= end_ms - protocol.end_exit_buffer_seconds * 1000:
            return "study_end_exit"
        if now_ms - self.opened_ms >= protocol.max_hold_hours * HOUR_MS:
            return "max_hold"
        if expiry_ms - now_ms <= protocol.close_before_expiry_hours * HOUR_MS:
            return "before_expiry"
        if not signal_eligible:
            return "signal_lost"
        if net_pnl is not None:
            if not net_pnl.is_finite():
                raise ValueError("平仓估值必须为有限数或 None")
            if net_pnl >= self.entry_debit * protocol.take_profit_debit_fraction:
                return "take_profit"
            if net_pnl <= -self.entry_debit * protocol.stop_loss_debit_fraction:
                return "stop_loss"
        return None

    # ---- 估值与结束 ----

    def net_value(
        self,
        quotes: Mapping[str, ExitQuote],
        protocol: SpreadProtocol,
    ) -> Decimal | None:
        """按可执行平仓价估值；任一持仓腿拿不到可执行价时返回 ``None``。"""
        if self.is_terminal and self.holding:
            return None
        amount = self.cash
        for held, symbol, sign in (
            (self.long_held, self.long_symbol, 1),
            (self.short_held, self.short_symbol, -1),
        ):
            if not held:
                continue
            if symbol is None:
                return None
            quote = quotes.get(symbol)
            if quote is None or not quote.price.is_finite() or quote.price <= 0:
                return None
            amount += sign * quote.price * self.quantity - protocol.option_fee(
                premium=quote.price, qty=self.quantity, index_price=quote.index_price
            )
        return amount

    def end_at_expiry(self) -> StudyPhase:
        """到期结算：有持仓则未解决，无持仓则取消。"""
        if self.is_terminal:
            return self.phase
        target = StudyPhase.EXPIRED_UNRESOLVED if self.holding else StudyPhase.CANCELLED
        self.transition(target)
        self.pending = None
        return target

    def end_at_study_end(self) -> StudyPhase:
        """研究结束：有持仓则未解决，无持仓则取消。"""
        if self.is_terminal:
            return self.phase
        target = StudyPhase.STUDY_ENDED_UNRESOLVED if self.holding else StudyPhase.CANCELLED
        self.transition(target)
        self.pending = None
        return target


__all__ = [
    "OPENING_PURPOSES",
    "TERMINAL_PHASES",
    "TRANSITIONS",
    "ExitQuote",
    "FillRecord",
    "IntentPurpose",
    "PendingIntent",
    "PositionLedger",
    "StudyPhase",
]
