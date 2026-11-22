"""Bybit V5 私有成交流 → BLSAE 双腿归约器的事件适配层。

本模块只做**纯适配**：把私有 ``order`` / ``execution`` 推送翻译成
:mod:`bybitbot.spread.execution` 的 :class:`PairEvent`，或者把事实汇总成
REST 全量校准的要求。它不持有连接、不访问数据库、不下单；网络层仍然是
:class:`bybitbot.adapters.bybit_ws.BybitPrivateStream`。

三条硬规则：

1. 只有挂在本次配对订单编号（``order_link_id``）上的事实才归约；
2. 每条物理订单的累计成交量单调不减；重复与乱序推送只能被吸收，
   不能生成第二次对冲动作；
3. 断线、鉴权失败、心跳超时、订阅失败、解析失败、积压或任何无法归属的事实
   都进入 :class:`RecoveryGate` 的 fail-closed 状态；只有 REST 对**订单、
   成交、仓位**三个维度完成全量校准后，才允许恢复新的对冲动作。

数量口径：``execution`` 是逐笔增量，按 ``order_link_id`` 累加得到该物理订单
的累计成交量；``order.cumExecQty`` 是同一订单的累计快照。两者交叉核验，
出现倒退只吸收状态不改数量；出现无法解释的差异（快照大于已见成交之和）
说明可能漏流，必须 fail-closed 并重新校准。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field, replace
from decimal import Decimal

from bybitbot.adapters.base import (
    ExecutionUpdate,
    ExecutionView,
    OrderUpdate,
    OrderView,
    PositionView,
)
from bybitbot.spread.execution import (
    EVENT_LEG_FILL,
    EVENT_LEG_TERMINAL,
    LEG_A,
    LEG_B,
    LEG_CANCELED,
    LEG_REJECTED,
    PairEvent,
)

ZERO = Decimal("0")

#: 这些交易所订单状态表示这条物理订单不再接受成交。
_TERMINAL_ORDER_STATUS = {
    "Filled",
    "Cancelled",
    "Canceled",
    "PartiallyFilledCanceled",
    "PartiallyFilledCancelled",
    "Rejected",
    "Deactivated",
    "Expired",
}
#: 能安全映射到腿状态机的终态。Deactivated/Expired 含义需 REST 核实，
#: 不能凭本地解释宣布"订单不存在"。
_TERMINAL_LEG_STATUS = {
    "Filled": "FILLED",
    "Cancelled": LEG_CANCELED,
    "Canceled": LEG_CANCELED,
    "PartiallyFilledCanceled": LEG_CANCELED,
    "PartiallyFilledCancelled": LEG_CANCELED,
    "Rejected": LEG_REJECTED,
}


class EventAdapterError(RuntimeError):
    """私有流事件无法安全归约；必须 fail-closed。"""


@dataclass(frozen=True)
class OrderIdentity:
    """一次配对中一条物理订单的身份。"""

    order_link_id: str
    leg: str
    #: 该物理订单的目标数量，用于识别"多出来的成交"。
    qty: Decimal

    def __post_init__(self) -> None:
        if self.leg not in (LEG_A, LEG_B):
            raise EventAdapterError(f"未知腿：{self.leg!r}")
        if self.qty <= 0:
            raise EventAdapterError("腿订单目标数量必须为正")


@dataclass(frozen=True)
class CalibrationSnapshot:
    """REST 全量校准的证据。

    三个维度必须齐全、且都要在断线/失效之后取得。缺任一项都不能解除门槛。
    """

    orders: tuple[OrderView, ...]
    executions: tuple[ExecutionView, ...]
    positions: tuple[PositionView, ...]
    #: 日历时间边界（epoch ms）。早于/等于该时刻的推送都是晚到。
    boundary_ms: int
    observed_at: dt.datetime

    @property
    def evidence(self) -> dict[str, object]:
        return {
            "orders": len(self.orders),
            "executions": len(self.executions),
            "positions": len(self.positions),
            "boundary_ms": self.boundary_ms,
            "observed_at": self.observed_at.isoformat(),
        }


@dataclass(frozen=True)
class RecoveryGate:
    """对冲动作的 fail-closed 门槛。

    初始状态和每次连接失效后都是 ``blocked=True``。``calibrate()`` 只在
    取得三维度 REST 全量快照且没有未解决的未知事实时解除。
    """

    blocked: bool = True
    reason: str = "私有流尚未完成全量 REST 校准"
    generation: int = 0
    calibration: CalibrationSnapshot | None = None

    @property
    def allows_new_actions(self) -> bool:
        return not self.blocked

    def block(self, reason: str) -> RecoveryGate:
        return replace(self, blocked=True, reason=reason, generation=self.generation + 1)

    def calibrated(self, snapshot: CalibrationSnapshot) -> RecoveryGate:
        return replace(
            self,
            blocked=False,
            reason="",
            generation=self.generation + 1,
            calibration=snapshot,
        )


@dataclass
class _OrderFacts:
    """一条物理订单上已确认的交易所事实。"""

    qty: Decimal
    cumulative: Decimal = ZERO
    avg_price: Decimal | None = None
    exec_ids: set[str] = field(default_factory=set)
    status: str | None = None
    terminal_reported: bool = False

    def add_execution(self, exec_id: str, qty: Decimal, price: Decimal) -> bool:
        """累加一笔成交。返回是否为新事实。"""
        if exec_id in self.exec_ids:
            return False
        self.exec_ids.add(exec_id)
        self.cumulative += qty
        self.avg_price = price if self.avg_price is None else (self.avg_price + price) / 2
        return True


@dataclass
class PrivateExecutionAdapter:
    """把私有流事件翻译成 BLSAE 归约事件。

    所有方法都是同步的：本类不创建任务、不读取本机时钟（时间戳从交易所事件
    携带），因此相同事件序列永远得到相同输出。上层把 websocket 迭代器里的
    ``OrderUpdate`` / ``ExecutionUpdate`` 逐条送入，并按 :attr:`gate` 决定
    是否允许新的对冲动作。
    """

    pair_trade_id: str
    symbol_a: str
    symbol_b: str
    #: 物理订单编号 → 腿。执行器在发单时调用 ``register_order`` 登记。
    orders: dict[str, OrderIdentity] = field(default_factory=dict)
    gate: RecoveryGate = field(default_factory=RecoveryGate)
    _facts: dict[str, _OrderFacts] = field(default_factory=dict)
    #: 校准边界：只有事件时间严格晚于该时刻的推送才被计入成交。
    #: 初始为 None 表示尚未取得 REST 全量证据，所有推送都不计入累计量。
    _accepted_boundary_ms: int | None = None

    def __post_init__(self) -> None:
        for identity in self.orders.values():
            self._facts.setdefault(identity.order_link_id, _OrderFacts(qty=identity.qty))

    # ------------------------------------------------------------------
    # 身份与门槛
    # ------------------------------------------------------------------
    def register_order(self, identity: OrderIdentity) -> None:
        prefix = f"{self.pair_trade_id}_"
        if not identity.order_link_id.startswith(prefix):
            raise EventAdapterError("订单编号不属于本次配对")
        existing = self.orders.get(identity.order_link_id)
        if existing is not None and existing != identity:
            raise EventAdapterError("同一订单编号被重复登记为不同订单")
        self.orders[identity.order_link_id] = identity
        self._facts.setdefault(identity.order_link_id, _OrderFacts(qty=identity.qty))

    def invalidate(self, reason: str) -> RecoveryGate:
        """断线、鉴权、心跳或解析异常：立即暂停所有新动作。"""
        self.gate = self.gate.block(reason)
        self._accepted_boundary_ms = None
        return self.gate

    def calibrate(self, snapshot: CalibrationSnapshot) -> RecoveryGate:
        """用 REST 对订单、成交与仓位做全量校准后解除门槛。

        校准不信任未被 REST 覆盖的推送事实：每条推送成交都必须能在 REST
        成交快照里找到对应 ``execId``；每条已登记订单都必须在订单快照里。
        任何一条缺失都继续 fail-closed，绝不"猜一个"累计成交量。
        """
        self._validate_snapshot(snapshot)
        self.gate = self.gate.calibrated(snapshot)
        self._accepted_boundary_ms = snapshot.boundary_ms
        # 用 REST 事实重建每条订单的累计成交量；晚到且不在快照中的推送不会
        # 改变它，因此不会重复放大已确认数量。
        rebuilt: dict[str, _OrderFacts] = {
            link: _OrderFacts(qty=identity.qty) for link, identity in self.orders.items()
        }
        for execution in snapshot.executions:
            link = execution.order_link_id or ""
            if link not in rebuilt:
                continue
            facts = rebuilt[link]
            facts.cumulative += execution.exec_qty
            facts.avg_price = (
                execution.exec_price
                if facts.avg_price is None
                else (facts.avg_price + execution.exec_price) / 2
            )
        for order in snapshot.orders:
            if order.order_link_id in rebuilt:
                rebuilt[order.order_link_id].status = order.status
                rebuilt[order.order_link_id].cumulative = max(
                    rebuilt[order.order_link_id].cumulative, order.qty_filled
                )
                if order.qty_filled > 0 and rebuilt[order.order_link_id].avg_price is None:
                    rebuilt[order.order_link_id].avg_price = order.avg_price
        self._facts = rebuilt
        return self.gate

    def _validate_snapshot(self, snapshot: CalibrationSnapshot) -> None:
        if not snapshot.orders and self.orders:
            raise EventAdapterError("REST 订单快照为空，不能校准已发出的腿订单")
        expected_symbols = {self.symbol_a, self.symbol_b}
        position_symbols = {position.symbol for position in snapshot.positions}
        if not expected_symbols.issubset(position_symbols):
            raise EventAdapterError("REST 仓位快照缺少套利腿合约")
        covered = {execution.exec_id for execution in snapshot.executions}
        for facts in self._facts.values():
            missing = facts.exec_ids - covered
            if missing:
                raise EventAdapterError("推送成交未出现在 REST 校准快照中")

    # ------------------------------------------------------------------
    # 事件翻译
    # ------------------------------------------------------------------
    def on_execution(self, update: ExecutionUpdate) -> list[PairEvent]:
        """处理一条私有 ``execution`` 事件，返回待归约的腿事件。"""
        if self.gate.blocked:
            return []
        identity = self._identity(update.order_link_id, update.symbol)
        if identity is None:
            return []  # 其他策略/配对的成交，不属于本次事实域。
        event_ms = (
            int(update.event_time.timestamp() * 1000) if update.event_time is not None else None
        )
        facts = self._facts[identity.order_link_id]
        if update.exec_qty <= 0 or update.exec_price <= 0:
            self.invalidate("私有成交事件数量或价格非法")
            return []
        if not facts.add_execution(update.exec_id, update.exec_qty, update.exec_price):
            return []  # 完全重复的推送不产生第二次对冲动作。
        # 校准前/校准边界内的成交只作为去重证据，不提升累计量；
        # 这些事实已由 REST 全量快照覆盖，晚到重放会造成重复对冲。
        if not self.gate.allows_new_actions or (
            self._accepted_boundary_ms is not None and event_ms <= self._accepted_boundary_ms
        ):
            facts.cumulative -= update.exec_qty
            return []
        if facts.cumulative > identity.qty:
            self.invalidate("推送累计成交超过订单数量")
            return []
        return [self._fill_event(identity, facts)]

    def on_order(self, update: OrderUpdate) -> list[PairEvent]:
        """处理一条私有 ``order`` 事件，返回待归约的腿事件。"""
        if self.gate.blocked:
            return []
        identity = self._identity(update.order_link_id, update.symbol)
        if identity is None:
            return []
        facts = self._facts[identity.order_link_id]
        # order 事件只更新状态，不新增成交。成交事实只能来自 execution，
        # 这样"撤单与成交竞态"下的重复 order 推送绝不会造成重复对冲。
        if update.qty_filled > facts.cumulative:
            # 交易所累计快照比已见逐笔成交多：说明漏了推送，必须重新校准。
            self.invalidate("order 累计成交高于逐笔成交，可能漏流")
            return []
        facts.status = update.status
        if update.status not in _TERMINAL_ORDER_STATUS or facts.terminal_reported:
            return []
        facts.terminal_reported = True
        leg_status = _TERMINAL_LEG_STATUS.get(update.status)
        if leg_status is None:
            self.invalidate(f"订单终态 {update.status} 需要 REST 校准")
            return []
        return [
            PairEvent(
                kind=EVENT_LEG_TERMINAL,
                leg=identity.leg,
                status=leg_status,
                qty_filled=facts.cumulative,
                avg_price=facts.avg_price,
            )
        ]

    @staticmethod
    def _fill_event(identity: OrderIdentity, facts: _OrderFacts) -> PairEvent:
        return PairEvent(
            kind=EVENT_LEG_FILL,
            leg=identity.leg,
            qty_filled=facts.cumulative,
            avg_price=facts.avg_price,
        )

    def _identity(self, order_link_id: str | None, symbol: str) -> OrderIdentity | None:
        if not order_link_id:
            return None
        identity = self.orders.get(order_link_id)
        if identity is None:
            return None
        expected = self.symbol_a if identity.leg == LEG_A else self.symbol_b
        if symbol != expected:
            self.invalidate("推送品种与配对腿不符")
            return None
        return identity

    def leg_filled_qty(self, leg: str) -> Decimal:
        """该腿所有物理订单的累计成交量（只读观测）。"""
        return sum(
            (
                facts.cumulative
                for link, facts in self._facts.items()
                if self.orders.get(link) is not None and self.orders[link].leg == leg
            ),
            ZERO,
        )


def reduce_private_events(
    execution, events: list[PairEvent], *, at_ms: int, quote_b=None
) -> tuple[object, list]:
    """把适配后的事件依次送入纯归约器。

    这是唯一的"桥"函数：仍然调用 :func:`bybitbot.spread.execution.reduce`，
    不在状态机里引入任何 I/O。重复事件已在适配层吸收，因此不会产生重复的
    ``SUBMIT`` 动作。
    """
    from bybitbot.spread.execution import reduce as pure_reduce

    state, actions = execution, []
    for event in events:
        stamped = replace(event, at_ms=at_ms, quote_b=quote_b or event.quote_b)
        state, produced = pure_reduce(state, stamped)
        actions.extend(produced)
    return state, actions


__all__ = [
    "CalibrationSnapshot",
    "EventAdapterError",
    "OrderIdentity",
    "PrivateExecutionAdapter",
    "RecoveryGate",
    "reduce_private_events",
]
