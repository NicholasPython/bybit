"""订单业务状态机与同步可信度（EXE-05 末段）。

需求原文：
    订单的业务状态和同步可信度分别保存。典型业务状态为
    ``PREPARED → SUBMITTING → ACCEPTED → PARTIALLY_FILLED → FILLED/CANCELED/REJECTED/EXPIRED``，
    允许跳过未观察到的中间态。``UNKNOWN`` 表示结果或同步状态不确定，
    可与部分成交事实并存；不能用它覆盖已确认的成交数量。

    ``EXPIRED`` 只用于已确认的交易所失效事实，不能因本地计时器到期直接写入。

三条硬不变量：
    1. **终态不可倒退** —— 旧事件不能把终态订单拉回中间态。
    2. **成交数量单调不减** —— 晚到的真实成交仍然入账，UNKNOWN 不能覆盖
       已确认的成交数量。
    3. **EXPIRED 必须有交易所事实** —— 本地 TTL 到期只触发撤单。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

#: 业务状态。
PREPARED = "PREPARED"
SUBMITTING = "SUBMITTING"
ACCEPTED = "ACCEPTED"
PARTIALLY_FILLED = "PARTIALLY_FILLED"
FILLED = "FILLED"
CANCELED = "CANCELED"
REJECTED = "REJECTED"
EXPIRED = "EXPIRED"
UNKNOWN = "UNKNOWN"

#: 终态集合。
TERMINAL_STATES: frozenset[str] = frozenset({FILLED, CANCELED, REJECTED, EXPIRED})

#: 同步可信度。
SYNC_NONE = "NONE"  # 从未交给交易所
SYNC_PENDING = "PENDING"  # 已发出，等待交易所事实
SYNC_CONFIRMED = "CONFIRMED"  # 交易所事实已确认
SYNC_UNCERTAIN = "UNCERTAIN"  # 结果不确定（UNKNOWN）

#: 合法迁移表。允许跳过未观察到的中间态：
#:   PREPARED → ACCEPTED 是合法的（提交很快，没观察到 SUBMITTING）。
_TRANSITIONS: dict[str, frozenset[str]] = {
    PREPARED: frozenset(
        {SUBMITTING, ACCEPTED, PARTIALLY_FILLED, FILLED, REJECTED, CANCELED, EXPIRED, UNKNOWN}
    ),
    SUBMITTING: frozenset(
        {ACCEPTED, PARTIALLY_FILLED, FILLED, REJECTED, CANCELED, EXPIRED, UNKNOWN}
    ),
    ACCEPTED: frozenset({PARTIALLY_FILLED, FILLED, CANCELED, EXPIRED, UNKNOWN}),
    PARTIALLY_FILLED: frozenset({PARTIALLY_FILLED, FILLED, CANCELED, EXPIRED, UNKNOWN}),
    # UNKNOWN 可以被任何交易所事实解决，但不能倒退已确认的成交数量。
    UNKNOWN: frozenset(
        {
            PREPARED,
            SUBMITTING,
            ACCEPTED,
            PARTIALLY_FILLED,
            FILLED,
            CANCELED,
            REJECTED,
            EXPIRED,
            UNKNOWN,
        }
    ),
    FILLED: frozenset({FILLED}),
    CANCELED: frozenset({CANCELED}),
    REJECTED: frozenset({REJECTED}),
    EXPIRED: frozenset({EXPIRED}),
}


class TransitionError(Exception):
    """非法状态迁移。"""


@dataclass(frozen=True)
class OrderState:
    """订单的业务状态 + 同步可信度 + 已确认成交数量。"""

    status: str
    sync_state: str
    qty_filled: Decimal = Decimal("0")

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATES

    @property
    def is_confirmed(self) -> bool:
        """交易所事实已确认。"""
        return self.sync_state == SYNC_CONFIRMED

    @property
    def is_uncertain(self) -> bool:
        """结果或同步状态不确定。"""
        return self.status == UNKNOWN or self.sync_state == SYNC_UNCERTAIN

    @property
    def was_ever_sent(self) -> bool:
        """是否曾经交给过交易所。

        EXE-02：只有「可证明发送器从未领取该请求」的本地持久化证据，
        才能证明订单没有被受理。``sync_state == NONE`` 就是那份证据。
        """
        return self.sync_state != SYNC_NONE


def can_transition(current: str, target: str) -> bool:
    """判断是否允许从 ``current`` 迁移到 ``target``。"""
    if current == target:
        return True
    return target in _TRANSITIONS.get(current, frozenset())


def apply_transition(
    state: OrderState,
    *,
    target: str,
    qty_filled: Decimal | None = None,
    confirmed: bool = False,
) -> OrderState:
    """应用一次状态迁移，返回新状态。

    Args:
        state: 当前状态。
        target: 目标业务状态。
        qty_filled: 本次事件携带的累计成交数量（交易所口径）。
        confirmed: 该事件是否来自交易所事实（REST 查询或私有推送）。

    Raises:
        TransitionError: 非法迁移，或违反「成交数量单调不减」。
    """
    if target not in _TRANSITIONS:
        raise TransitionError(f"未知状态 {target}")

    if target == EXPIRED and not confirmed:
        raise TransitionError("EXPIRED 只能由已确认的交易所失效事实写入")
    new_filled = state.qty_filled if qty_filled is None else max(state.qty_filled, qty_filled)
    # 旧快照不倒退终态，但晚到累计成交仍必须保留。取消后完整成交可升级 FILLED。
    if state.is_terminal:
        terminal = FILLED if target == FILLED and new_filled > state.qty_filled else state.status
        return OrderState(terminal, SYNC_CONFIRMED if confirmed else state.sync_state, new_filled)
    if state.status == PARTIALLY_FILLED and target == ACCEPTED:
        target = PARTIALLY_FILLED
    if not can_transition(state.status, target):
        raise TransitionError(f"非法迁移 {state.status} → {target}")

    if target == UNKNOWN:
        sync = SYNC_UNCERTAIN
    elif confirmed:
        sync = SYNC_CONFIRMED
    elif state.sync_state == SYNC_NONE and target == SUBMITTING:
        sync = SYNC_PENDING
    elif state.sync_state == SYNC_NONE and target == PREPARED:
        sync = SYNC_NONE
    else:
        sync = state.sync_state

    # 业务状态与成交数量必须自洽：全成不可能还挂在部分成交。
    if target == FILLED and new_filled == state.qty_filled and state.status != FILLED:
        # 交易所确认了 FILLED 但没带成交数量 —— 不改数量，只改状态。
        pass

    return OrderState(status=target, sync_state=sync, qty_filled=new_filled)


def effective_status(*, exchange_status: str, qty: Decimal, qty_filled: Decimal) -> str:
    """把交易所的订单状态映射成业务状态。

    Bybit 的状态值包含 ``New`` / ``PartiallyFilled`` / ``Filled`` /
    ``Cancelled`` / ``Rejected`` / ``Deactivated`` / ``Expired`` 等，
    这里按语义归一。

    注意 ``Deactivated``：这是交易所确认的失效事实，映射为 ``EXPIRED``，
    但只有在确实来自交易所时才允许写入（见 :func:`apply_transition`）。
    """
    normalized = exchange_status.strip().lower().replace("_", "")
    # V5 官方枚举：未终结 = New / PartiallyFilled / Untriggered（+ Triggered
    # 是 Untriggered→New 的瞬时态）；已终结 = Filled / Cancelled / Rejected /
    # PartiallyFilledCanceled / Deactivated。
    if normalized in {"new", "created", "untriggered", "triggered"}:
        return PARTIALLY_FILLED if qty_filled > 0 else ACCEPTED
    if normalized == "partiallyfilled":
        return PARTIALLY_FILLED
    if normalized == "filled":
        return FILLED
    if normalized in {
        "cancelled",
        "canceled",
        "partiallyfilledcanceled",
        "partiallyfilledcancelled",
    }:
        return CANCELED
    if normalized == "deactivated":
        # Deactivated 是交易所确认的失效事实（UTA 条件单在触发前被撤）。
        return EXPIRED
    if normalized == "rejected":
        return REJECTED
    if normalized == "expired":
        return EXPIRED
    return UNKNOWN


__all__ = [
    "ACCEPTED",
    "CANCELED",
    "EXPIRED",
    "FILLED",
    "PARTIALLY_FILLED",
    "PREPARED",
    "REJECTED",
    "SUBMITTING",
    "SYNC_CONFIRMED",
    "SYNC_NONE",
    "SYNC_PENDING",
    "SYNC_UNCERTAIN",
    "TERMINAL_STATES",
    "UNKNOWN",
    "OrderState",
    "TransitionError",
    "apply_transition",
    "can_transition",
    "effective_status",
]
