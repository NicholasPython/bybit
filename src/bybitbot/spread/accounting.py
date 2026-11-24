"""从不可变成交重建配对持仓与已实现盈亏；不依赖订单累计快照。"""

from dataclasses import dataclass, field
from decimal import Decimal

from bybitbot.spread.models import SpreadExecution, SpreadLegOrder

ZERO = Decimal("0")
TERMINAL_ORDERS = {"FILLED", "CANCELED", "REJECTED"}
TERMINAL_PAIRS = {"CLOSED", "ABORTED", "ERROR_UNHEDGED"}


@dataclass
class PairFacts:
    opened: dict[str, Decimal] = field(default_factory=lambda: {"A": ZERO, "B": ZERO})
    closed: dict[str, Decimal] = field(default_factory=lambda: {"A": ZERO, "B": ZERO})
    entry_value: dict[str, Decimal] = field(default_factory=lambda: {"A": ZERO, "B": ZERO})
    close_value: dict[str, Decimal] = field(default_factory=lambda: {"A": ZERO, "B": ZERO})
    fees: Decimal = ZERO
    realised: Decimal = ZERO

    def remaining(self, leg):
        return self.opened[leg] - self.closed[leg]

    def average(self, leg):
        return self.entry_value[leg] / self.opened[leg] if self.opened[leg] else None


def pair_facts(orders: list[SpreadLegOrder], executions: list[SpreadExecution]) -> PairFacts:
    facts = PairFacts()
    by_id = {order.id: order for order in orders}
    for fill in executions:
        order = by_id.get(fill.order_id)
        if order is None:
            continue
        leg = order.leg
        if order.role == "FLATTEN":
            facts.closed[leg] += fill.exec_qty
            facts.close_value[leg] += fill.exec_qty * fill.exec_price
        else:
            facts.opened[leg] += fill.exec_qty
            facts.entry_value[leg] += fill.exec_qty * fill.exec_price
        facts.fees += fill.exec_fee
    for leg in ("A", "B"):
        if facts.closed[leg] > facts.opened[leg]:
            raise ValueError("平仓成交超过本配对归属持仓")
        if facts.closed[leg]:
            side = next(
                order.side for order in orders if order.leg == leg and order.role != "FLATTEN"
            )
            pnl = facts.close_value[leg] - facts.closed[leg] * facts.average(leg)
            facts.realised += pnl if side == "Buy" else -pnl
    return facts
