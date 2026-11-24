"""双腿原子执行与「瘸腿自愈」（PRD FR-02）。

核心设计
--------
执行流程用**事件归约**（event reduction）表达：``reduce(execution, event)``
是纯函数，返回新的执行状态与一组待执行的动作。这与本仓既有的
「风控判定是纯函数、执行器另行落地」是同一套口径，好处是：

* 双腿时序（先弱后强、部分成交、超时、追单、反向平仓）全部可以单测复现；
* 状态迁移与动作产出分离，换执行后端（REST 轮询 → 私有 WebSocket）
  不需要改状态机。

时序（PRD FR-02.1 / FR-02.2 / FR-02.3）::

    ① 识别流动性较差的一腿为 Leg A（被动腿），流动性较好的为 Leg B（主动腿）；
    ② 向 Leg A 挂 Post-Only 限价单；
    ③ Leg A 一旦有成交（哪怕是部分成交），立即按等名义价值计算 Leg B 的数量，
       以 IOC 保护价向 Leg B 发单吃单；
    ④ Leg A 超过被动超时仍零成交 → 撤单、放弃本次开仓；
    ⑤ Leg B 的 IOC 未完全成交 → 在恢复窗口内追单补偿；
       若追单滑点超出容忍度 → 立即反向平掉 Leg A 的已成交头寸。

数量口径
--------
一条腿可能由**多笔物理订单**组成（首单 + 若干次追单）。因此：

* ``qty`` —— 当前这笔物理订单的目标数量；
* ``qty_target`` —— 该腿所有物理订单的目标数量累计；
* ``qty_filled`` —— 交易所口径的累计成交量（单调不减）。

**能力边界（必须说清楚）**：15 毫秒级对冲（PRD 非功能需求）依赖 Bybit 私有
WebSocket 的 ``execution`` 推送。本仓当前私有流尚未接入
（差距评估 §3.4），实盘只能靠 REST 轮询，实际延迟是秒级。因此本模块把
「成交事件来源」做成可替换的接口：阶段一用轮询驱动，阶段二接上私有流
即可获得毫秒级对冲。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from decimal import Decimal

from bybitbot.spread.states import (
    ABORTED,
    CLOSING,
    ERROR_UNHEDGED,
    HEDGED,
    OPENING,
    PairState,
    apply_transition,
    equal_notional_qty,
    quantize_qty,
    slippage_bps,
)

#: 腿标识。
LEG_A = "A"
LEG_B = "B"

#: 腿在本次配对中的角色。
ROLE_PASSIVE = "PASSIVE"  # Leg A：流动性较差的一腿，Post-Only 挂单
ROLE_ACTIVE = "ACTIVE"  # Leg B：流动性较好的一腿，IOC 吃单对冲
ROLE_RECOVERY = "RECOVERY"  # 追单补偿
ROLE_FLATTEN = "FLATTEN"  # 反向平仓

#: 腿的物理状态。
LEG_PENDING = "PENDING"  # 尚未发出
LEG_WORKING = "WORKING"  # 已发出，等待成交
LEG_PARTIAL = "PARTIAL"  # 部分成交
LEG_FILLED = "FILLED"  # 完全成交
LEG_CANCELED = "CANCELED"
LEG_REJECTED = "REJECTED"
LEG_UNKNOWN = "UNKNOWN"  # 结果不确定

#: 事件类型。
EVENT_LEG_FILL = "LEG_FILL"
EVENT_LEG_TERMINAL = "LEG_TERMINAL"
EVENT_DEADLINE = "DEADLINE"
EVENT_TICK = "TICK"
EVENT_CLOSE = "CLOSE"

#: 动作类型。
ACTION_SUBMIT = "SUBMIT"
ACTION_CANCEL = "CANCEL"
ACTION_CLOSE = "CLOSE"
ACTION_ALERT = "ALERT"

_ORDER_LINK_ID = re.compile(r"[A-Za-z0-9_-]{1,36}")


def order_link_id(*, pair_trade_id: str, leg: str, sequence: int) -> str:
    """生成幂等订单编号（PRD FR-04.1）。

    格式 ``{pair_trade_id}_{leg}{序号}``，长度不超过 Bybit 的 36 字符上限。
    **同一笔逻辑订单的所有重试必须复用同一个编号** —— 序号只在产生新的
    逻辑订单（首挂、追单、平仓）时递增。
    """
    value = f"{pair_trade_id}_{leg}{sequence}"
    if not _ORDER_LINK_ID.fullmatch(value):
        raise ValueError(f"订单编号不符合 Bybit 约束：{value!r}")
    return value


@dataclass(frozen=True)
class LegQuote:
    """一条腿的最优报价。"""

    symbol: str
    bid: Decimal
    ask: Decimal
    bid_qty: Decimal = Decimal("0")
    ask_qty: Decimal = Decimal("0")

    def take_price(self, side: str) -> Decimal:
        """吃单价：买入取卖一，卖出取买一。"""
        return self.ask if side.lower() == "buy" else self.bid


@dataclass(frozen=True)
class PairSignal:
    """一次开仓信号与该配对的执行约束。"""

    pair_trade_id: str
    pair_key: str
    strategy_id: str
    symbol_a: str
    symbol_b: str
    #: ``SHORT_A_LONG_B`` 或 ``LONG_A_SHORT_B``。
    direction: str
    #: 本次开仓的目标名义价值。
    notional: Decimal
    #: Leg A 的挂单价（Post-Only）。
    passive_price: Decimal
    #: Leg B 的保护价（IOC）。
    active_price: Decimal
    qty_step_a: Decimal
    qty_step_b: Decimal
    #: Leg A 侧方向。
    side_a: str = "Buy"
    #: Leg B 侧方向。
    side_b: str = "Sell"
    #: 被动腿挂单超时（PRD FR-02.3：2000ms）。
    passive_timeout_ms: int = 2000
    #: 追单补偿窗口（PRD FR-02.3：500ms）。
    recovery_window_ms: int = 5000
    #: 追单允许的最大滑点（基点）；超出即改为反向平仓。
    max_slippage_bps: Decimal = Decimal("50")
    #: 裸敞口名义上限（PRD FR-03.1）。
    max_unhedged_notional: Decimal = Decimal("50")
    #: 裸敞口宽限时间（PRD FR-03.1：5 秒）。
    unhedged_grace_ms: int = 5000


@dataclass
class LegExecution:
    """一条腿的执行事实。"""

    leg: str
    role: str
    symbol: str
    side: str
    price: Decimal
    qty: Decimal
    #: 该腿所有物理订单的目标数量累计。
    qty_target: Decimal = Decimal("0")
    time_in_force: str = "GTC"
    status: str = LEG_PENDING
    qty_filled: Decimal = Decimal("0")
    avg_price: Decimal | None = None
    order_link_id: str | None = None
    sequence: int = 0
    #: 追单滑点的参考价（首单的保护价）。
    reference_price: Decimal | None = None
    #: 已发起的追单次数。
    chase_attempts: int = 0
    #: 首次观测到对冲缺口的时刻。
    shortfall_ms: int | None = None
    last_error: str = ""

    @property
    def notional(self) -> Decimal:
        if self.avg_price is None:
            return Decimal("0")
        return self.qty_filled * self.avg_price


@dataclass
class PairExecution:
    """一次配对开仓的完整执行上下文。"""

    signal: PairSignal
    state: PairState = field(default_factory=lambda: PairState(OPENING))
    legs: dict[str, LegExecution] = field(default_factory=dict)
    opened_ms: int = 0
    passive_deadline_ms: int = 0
    #: 首次观测到裸敞口越界的时刻。
    first_unhedged_ms: int | None = None
    closed_ms: int | None = None
    last_error: str = ""

    @property
    def pair_trade_id(self) -> str:
        return self.signal.pair_trade_id

    @property
    def leg_a(self) -> LegExecution:
        return self.legs[LEG_A]

    @property
    def leg_b(self) -> LegExecution:
        return self.legs[LEG_B]

    @property
    def unhedged(self) -> Decimal:
        return abs(self.leg_a.notional - self.leg_b.notional)


@dataclass(frozen=True)
class PairEvent:
    """驱动一次归约的事件。"""

    kind: str
    leg: str | None = None
    #: 交易所口径的**累计**成交量。
    qty_filled: Decimal = Decimal("0")
    avg_price: Decimal | None = None
    status: str | None = None
    at_ms: int = 0
    #: Leg B 的最新报价，用于计算追单保护价。
    quote_b: LegQuote | None = None


@dataclass(frozen=True)
class LegAction:
    """归约产出的待执行动作。"""

    kind: str
    leg: str
    role: str
    qty: Decimal = Decimal("0")
    price: Decimal | None = None
    order_link_id: str | None = None
    reason: str = ""


def new_execution(signal: PairSignal, *, now_ms: int) -> PairExecution:
    """建立一次配对开仓的执行上下文。"""
    qty_a = quantize_qty(signal.notional / signal.passive_price, signal.qty_step_a)
    legs = {
        LEG_A: LegExecution(
            leg=LEG_A,
            role=ROLE_PASSIVE,
            symbol=signal.symbol_a,
            side=signal.side_a,
            price=signal.passive_price,
            qty=qty_a,
            qty_target=qty_a,
            time_in_force="PostOnly",
            order_link_id=order_link_id(pair_trade_id=signal.pair_trade_id, leg=LEG_A, sequence=0),
            reference_price=signal.passive_price,
        ),
        LEG_B: LegExecution(
            leg=LEG_B,
            role=ROLE_ACTIVE,
            symbol=signal.symbol_b,
            side=signal.side_b,
            price=signal.active_price,
            qty=Decimal("0"),
            qty_target=Decimal("0"),
            time_in_force="IOC",
            reference_price=signal.active_price,
        ),
    }
    return PairExecution(
        signal=signal,
        legs=legs,
        opened_ms=now_ms,
        passive_deadline_ms=now_ms + signal.passive_timeout_ms,
    )


def _apply_fill(leg: LegExecution, event: PairEvent) -> LegExecution:
    """把一条腿的成交事实合并进执行状态（成交数量单调不减）。"""
    if event.qty_filled < leg.qty_filled:
        raise ValueError("成交数量不可回撤")
    updated = replace(leg, qty_filled=event.qty_filled)
    if event.avg_price is not None and event.qty_filled > leg.qty_filled:
        updated.avg_price = event.avg_price
    if leg.qty_target > 0 and event.qty_filled >= leg.qty_target:
        updated.status = LEG_FILLED
    elif event.qty_filled > 0:
        updated.status = LEG_PARTIAL
    return updated


def _required_qty_b(signal: PairSignal, notional_a: Decimal) -> Decimal:
    """按等名义价值计算腿 B 的目标数量。"""
    return equal_notional_qty(
        filled_notional_a=notional_a,
        price_b=signal.active_price,
        qty_step_b=signal.qty_step_b,
    )


def _chase_price(
    signal: PairSignal, leg: LegExecution, quote: LegQuote | None
) -> tuple[Decimal | None, str]:
    """计算追单保护价。

    Returns:
        ``(价格, 原因)``。价格为 ``None`` 表示滑点超出容忍度，只能反向平仓。
    """
    if quote is None:
        return None, "缺少报价，无法计算追单价"
    reference = leg.reference_price or leg.price
    price = quote.take_price(leg.side)
    if price <= 0:
        return None, "报价无效"
    if slippage_bps(reference, price) > signal.max_slippage_bps:
        return None, "追单滑点超出容忍度"
    return price, ""


def _flatten_excess(
    signal: PairSignal, leg_a: LegExecution, leg_b: LegExecution, reason: str
) -> LegAction:
    """生成"平掉多余敞口"的动作。

    名义价值大的一侧才是多余敞口：多出来的部分必须被反向平掉，
    否则净敞口会一直挂在账户上。
    """
    if leg_a.notional >= leg_b.notional:
        leg, excess, price, step = (
            LEG_A,
            leg_a.notional - leg_b.notional,
            leg_a.avg_price,
            (signal.qty_step_a),
        )
    else:
        leg, excess, price, step = (
            LEG_B,
            leg_b.notional - leg_a.notional,
            leg_b.avg_price,
            (signal.qty_step_b),
        )
    qty = Decimal("0")
    if price and price > 0:
        qty = quantize_qty(excess / price, step)
    source = leg_a if leg == LEG_A else leg_b
    return LegAction(
        kind=ACTION_CLOSE,
        leg=leg,
        role=ROLE_FLATTEN,
        qty=qty or source.qty_filled,
        price=price,
        reason=reason,
    )


def _to_error_unhedged(state: PairState) -> PairState:
    """进入不可修复的异常终态。已是终态则保持不变。"""
    if state.is_terminal:
        return state
    return apply_transition(state, target=ERROR_UNHEDGED)


def reduce(execution: PairExecution, event: PairEvent) -> tuple[PairExecution, list[LegAction]]:
    """归约一次事件，返回新状态与待执行动作。

    纯函数：同样的输入永远得到同样的输出，不写库、不调交易所。
    """
    actions: list[LegAction] = []
    signal = execution.signal
    legs = dict(execution.legs)
    state = execution.state
    now = event.at_ms

    if event.kind == EVENT_LEG_FILL:
        if event.leg not in (LEG_A, LEG_B):
            raise ValueError(f"未知腿：{event.leg!r}")
        legs[event.leg] = _apply_fill(legs[event.leg], event)

    elif event.kind == EVENT_LEG_TERMINAL:
        if event.leg not in (LEG_A, LEG_B):
            raise ValueError(f"未知腿：{event.leg!r}")
        legs[event.leg] = replace(legs[event.leg], status=event.status or legs[event.leg].status)

    elif event.kind == EVENT_CLOSE:
        state = apply_transition(state, target=CLOSING)
        for leg_key in (LEG_A, LEG_B):
            leg = legs[leg_key]
            if leg.qty_filled > 0:
                actions.append(
                    LegAction(
                        kind=ACTION_CLOSE,
                        leg=leg_key,
                        role=ROLE_FLATTEN,
                        qty=leg.qty_filled,
                        reason="配对平仓",
                    )
                )
        return replace(execution, state=state, legs=legs), actions

    elif event.kind not in (EVENT_DEADLINE, EVENT_TICK):
        raise ValueError(f"未知事件类型：{event.kind!r}")

    leg_a = legs[LEG_A]
    leg_b = legs[LEG_B]

    # --- 腿 A 成交：立即按等名义价值对冲腿 B -----------------------------
    if leg_a.qty_filled > 0 and leg_b.status == LEG_PENDING:
        target_b = _required_qty_b(signal, leg_a.notional)
        if target_b > 0:
            sequence = leg_b.sequence + 1
            legs[LEG_B] = replace(
                leg_b,
                qty=target_b,
                qty_target=leg_b.qty_target + target_b,
                status=LEG_WORKING,
                sequence=sequence,
                order_link_id=order_link_id(
                    pair_trade_id=signal.pair_trade_id, leg=LEG_B, sequence=sequence
                ),
            )
            actions.append(
                LegAction(
                    kind=ACTION_SUBMIT,
                    leg=LEG_B,
                    role=ROLE_ACTIVE,
                    qty=target_b,
                    price=signal.active_price,
                    order_link_id=legs[LEG_B].order_link_id,
                    reason="腿 A 成交后按等名义价值对冲",
                )
            )
            leg_b = legs[LEG_B]

    # --- 腿 B 成交缺口：追单补偿或反向平仓 -------------------------------
    required = _required_qty_b(signal, leg_a.notional)
    residual = required - leg_b.qty_filled
    if (
        leg_a.qty_filled > 0
        and residual > 0
        and leg_b.status
        not in {
            LEG_PENDING,
            LEG_WORKING,
        }
    ):
        if leg_b.shortfall_ms is None:
            legs[LEG_B] = replace(leg_b, shortfall_ms=now)
            leg_b = legs[LEG_B]
        within_window = now - leg_b.shortfall_ms <= signal.recovery_window_ms
        price, reason = _chase_price(signal, leg_b, event.quote_b)
        if within_window and price is not None:
            sequence = leg_b.sequence + 1
            legs[LEG_B] = replace(
                leg_b,
                qty=residual,
                qty_target=leg_b.qty_target + residual,
                price=price,
                status=LEG_WORKING,
                role=ROLE_RECOVERY,
                sequence=sequence,
                chase_attempts=leg_b.chase_attempts + 1,
                order_link_id=order_link_id(
                    pair_trade_id=signal.pair_trade_id, leg=LEG_B, sequence=sequence
                ),
            )
            actions.append(
                LegAction(
                    kind=ACTION_SUBMIT,
                    leg=LEG_B,
                    role=ROLE_RECOVERY,
                    qty=residual,
                    price=price,
                    order_link_id=legs[LEG_B].order_link_id,
                    reason="对冲缺口追单补偿",
                )
            )
        else:
            actions.append(
                LegAction(
                    kind=ACTION_ALERT,
                    leg=LEG_B,
                    role=ROLE_RECOVERY,
                    qty=residual,
                    reason=reason or "追单窗口已过",
                )
            )
            actions.append(_flatten_excess(signal, leg_a, leg_b, "对冲缺口无法在窗口内补齐"))
            state = _to_error_unhedged(state)
        leg_b = legs[LEG_B]

    # --- 被动腿超时：零成交即放弃，已有成交必须先对冲 ---------------------
    if event.kind == EVENT_DEADLINE and state.state == OPENING:
        if leg_a.qty_filled == 0:
            actions.append(
                LegAction(
                    kind=ACTION_CANCEL,
                    leg=LEG_A,
                    role=ROLE_PASSIVE,
                    qty=leg_a.qty,
                    order_link_id=leg_a.order_link_id,
                    reason="被动腿超时未成交，放弃本次开仓",
                )
            )
            state = apply_transition(state, target=ABORTED)
        else:
            actions.append(
                LegAction(
                    kind=ACTION_CANCEL,
                    leg=LEG_A,
                    role=ROLE_PASSIVE,
                    qty=leg_a.qty,
                    order_link_id=leg_a.order_link_id,
                    reason="被动腿超时，撤销剩余挂单",
                )
            )

    # --- 裸敞口熔断（PRD FR-03.1）---------------------------------------
    unhedged = abs(leg_a.notional - leg_b.notional)
    first_unhedged = execution.first_unhedged_ms
    if unhedged > signal.max_unhedged_notional:
        if first_unhedged is None:
            first_unhedged = now
        if now - first_unhedged >= signal.unhedged_grace_ms:
            actions.append(_flatten_excess(signal, leg_a, leg_b, "单腿裸敞口持续超限"))
            state = _to_error_unhedged(state)
    else:
        # 敞口回到上限以内就把计时器清零：下一次越界必须重新计满宽限期，
        # 否则早先的一次瞬时越界会让后面每一次越界都立刻触发强平。
        first_unhedged = None

    # --- 双腿建立 → 已对冲 ------------------------------------------------
    if (
        state.state == OPENING
        and leg_a.qty_filled > 0
        and leg_b.qty_filled > 0
        and unhedged <= signal.max_unhedged_notional
    ):
        state = apply_transition(
            state,
            target=HEDGED,
            filled_qty_a=leg_a.qty_filled,
            filled_qty_b=leg_b.qty_filled,
            notional_a=leg_a.notional,
            notional_b=leg_b.notional,
            avg_price_a=leg_a.avg_price,
            avg_price_b=leg_b.avg_price,
            max_unhedged_notional=signal.max_unhedged_notional,
        )

    return (
        replace(execution, state=state, legs=legs, first_unhedged_ms=first_unhedged),
        actions,
    )


__all__ = [
    "ACTION_ALERT",
    "ACTION_CANCEL",
    "ACTION_CLOSE",
    "ACTION_SUBMIT",
    "EVENT_CLOSE",
    "EVENT_DEADLINE",
    "EVENT_LEG_FILL",
    "EVENT_LEG_TERMINAL",
    "EVENT_TICK",
    "LEG_A",
    "LEG_B",
    "LEG_CANCELED",
    "LEG_FILLED",
    "LEG_PARTIAL",
    "LEG_PENDING",
    "LEG_REJECTED",
    "LEG_UNKNOWN",
    "LEG_WORKING",
    "ROLE_ACTIVE",
    "ROLE_FLATTEN",
    "ROLE_PASSIVE",
    "ROLE_RECOVERY",
    "LegAction",
    "LegExecution",
    "LegQuote",
    "PairEvent",
    "PairExecution",
    "PairSignal",
    "new_execution",
    "order_link_id",
    "reduce",
]
