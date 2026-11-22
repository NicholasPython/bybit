"""线性腿三维校准与独立现金重放。任何不明事实均阻断执行，不覆盖归属。"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import select, text

from bybitbot.clock import now_utc
from bybitbot.db.models import ReconciliationRun, RuntimeState
from bybitbot.reconciliation import Difference, ReconcileResult
from bybitbot.spread.accounting import TERMINAL_ORDERS, pair_facts
from bybitbot.spread.models import (
    SpreadExecution,
    SpreadLegOrder,
    SpreadPairTrade,
    SpreadReconciliationLog,
)

ZERO = Decimal("0")
TOLERANCE = Decimal("0.000000000001")
_STATUS = {
    "New": "WORKING",
    "Created": "WORKING",
    "Accepted": "WORKING",
    "PartiallyFilled": "PARTIAL",
    "Filled": "FILLED",
    "Rejected": "REJECTED",
    "Cancelled": "CANCELED",
    "Canceled": "CANCELED",
    "PartiallyFilledCanceled": "CANCELED",
    "PartiallyFilledCancelled": "CANCELED",
    "Deactivated": "CANCELED",
    "Expired": "CANCELED",
}


class SpreadReconciler:
    def __init__(
        self, *, adapter, sessionmaker, environment, account_uid, record_interval_seconds=300
    ):
        self.adapter, self.sessionmaker = adapter, sessionmaker
        self.environment, self.account_uid = environment, account_uid
        self.record_interval_seconds = record_interval_seconds

    async def reconcile(self) -> ReconcileResult:
        result = ReconcileResult("spread")
        started = now_utc()
        record = True
        async with self.sessionmaker() as session:
            earliest = await session.scalar(
                select(SpreadPairTrade.opened_at)
                .where(SpreadPairTrade.environment == self.environment)
                .order_by(SpreadPairTrade.opened_at)
                .limit(1)
            )
        try:
            # 仅 paper 使用：完整历史，无远端分页/七天查询窗口的隐含假设。
            orders = await self.adapter.get_open_orders("linear")
            history = await self.adapter.get_order_history("linear")
            fills = await self.adapter.get_executions(
                "linear", start_time=(earliest - dt.timedelta(seconds=1)) if earliest else None
            )
            positions = await self.adapter.get_positions("linear")
            account = await self.adapter.get_account()
            async with self.sessionmaker() as session:
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                    {"key": f"{self.environment}:spread"},
                )
                pairs = list(
                    await session.scalars(
                        select(SpreadPairTrade).where(
                            SpreadPairTrade.environment == self.environment
                        )
                    )
                )
                local = list(
                    await session.scalars(
                        select(SpreadLegOrder).where(SpreadLegOrder.environment == self.environment)
                    )
                )
                known = {row.order_link_id: row for row in local}
                views = {view.order_link_id: view for view in [*history, *orders]}
                for view in views.values():
                    if view.order_link_id not in known:
                        self._difference(result, "EXTERNAL_LINEAR_ORDER", view.symbol)
                stored = list(
                    await session.scalars(
                        select(SpreadExecution).where(
                            SpreadExecution.environment == self.environment
                        )
                    )
                )
                by_exec = {fill.exec_id: fill for fill in stored}
                for fill in sorted(fills, key=lambda item: (item.exec_time, item.exec_id)):
                    row = known.get(fill.order_link_id)
                    if row is None:
                        self._difference(result, "EXTERNAL_LINEAR_EXECUTION", fill.symbol)
                        continue
                    view = views.get(row.order_link_id)
                    if (
                        not self._valid_fill(row, fill)
                        or view is not None
                        and fill.order_id != view.order_id
                    ):
                        self._difference(result, "INVALID_LINEAR_EXECUTION", row.symbol)
                        continue
                    old = by_exec.get(fill.exec_id)
                    values = (
                        row.id,
                        fill.exec_qty,
                        fill.exec_price,
                        fill.exec_fee,
                        fill.exec_fee_asset,
                        fill.exec_time,
                    )
                    if old:
                        if (
                            old.order_id,
                            old.exec_qty,
                            old.exec_price,
                            old.exec_fee,
                            old.exec_fee_asset,
                            old.exec_time,
                        ) != values:
                            self._difference(result, "EXECUTION_ID_CONFLICT", row.symbol)
                        continue
                    qty = sum((f.exec_qty for f in stored if f.order_id == row.id), ZERO)
                    if qty + fill.exec_qty > row.qty:
                        self._difference(result, "EXECUTION_OVERFILL", row.symbol)
                        continue
                    fact = SpreadExecution(
                        environment=self.environment,
                        order_id=row.id,
                        exec_id=fill.exec_id,
                        exec_qty=fill.exec_qty,
                        exec_price=fill.exec_price,
                        exec_fee=fill.exec_fee,
                        exec_fee_asset=fill.exec_fee_asset,
                        exec_time=fill.exec_time,
                    )
                    session.add(fact)
                    stored.append(fact)
                    by_exec[fill.exec_id] = fact
                    result.repaired = True
                for row in local:
                    view = views.get(row.order_link_id)
                    facts = [f for f in stored if f.order_id == row.id]
                    qty = sum((f.exec_qty for f in facts), ZERO)
                    if view is None:
                        if row.submitted_at is None and row.status in {"PENDING", "CANCELED"}:
                            continue  # 从未发送，有本地证据。
                        if row.status == "REJECTED" and row.sync_state == "CONFIRMED":
                            continue  # 确定拒绝，交易所可不生成历史。
                        row.status, row.sync_state = "UNKNOWN", "UNCERTAIN"
                        self._difference(result, "LINEAR_ORDER_NOT_FOUND", row.symbol)
                        continue
                    if not self._valid_order(row, view):
                        row.sync_state = "UNCERTAIN"
                        self._difference(result, "LINEAR_ORDER_IDENTITY", row.symbol)
                        continue
                    if (
                        view.qty_filled < row.qty_filled
                        or view.qty_filled != qty
                        or _STATUS.get(view.status) is None
                        or (
                            row.status in TERMINAL_ORDERS
                            and _STATUS.get(view.status) not in TERMINAL_ORDERS
                        )
                    ):
                        row.sync_state = "UNCERTAIN"
                        self._difference(result, "LINEAR_ORDER_FACTS_PENDING", row.symbol)
                        continue
                    row.exchange_order_id = view.order_id
                    row.qty_filled = qty
                    row.avg_price = (
                        sum((f.exec_qty * f.exec_price for f in facts), ZERO) / qty if qty else None
                    )
                    row.status, row.sync_state = _STATUS[view.status], "CONFIRMED"
                expected = {}
                realised = fees = ZERO
                for pair in pairs:
                    if pair.account_uid != self.account_uid or not pair.context:
                        self._difference(result, "PAIR_CONTEXT_UNTRUSTED", pair.pair_trade_id)
                    pair_orders = [row for row in local if row.pair_trade_id == pair.id]
                    facts = pair_facts(pair_orders, stored)
                    pair.filled_qty_a, pair.filled_qty_b = facts.opened["A"], facts.opened["B"]
                    pair.avg_price_a, pair.avg_price_b = facts.average("A"), facts.average("B")
                    pair.notional_a, pair.notional_b = (
                        facts.entry_value["A"],
                        facts.entry_value["B"],
                    )
                    pair.spread_pnl, pair.trading_fee = facts.realised, facts.fees
                    pair.net_pnl = facts.realised + pair.funding_pnl - facts.fees
                    if pair.closed_at and any(facts.remaining(leg) > 0 for leg in ("A", "B")):
                        # 晚到事实不倒退终态，但恢复持仓监控与安全退出。
                        pair.closed_at = None
                        pair.context = {**pair.context, "close_requested": True}
                        pair.last_error = "终态后发现晚到成交，继续核实并退出归属持仓"
                    realised += pair.spread_pnl
                    fees += pair.trading_fee
                    for leg, symbol, side in (
                        ("A", pair.symbol_a, pair.side_a),
                        ("B", pair.symbol_b, pair.side_b),
                    ):
                        expected[symbol] = expected.get(symbol, ZERO) + facts.remaining(leg) * (
                            1 if side == "Buy" else -1
                        )
                actual = {}
                for position in positions:
                    if position.size == 0:
                        continue
                    if (
                        position.position_idx != 0
                        or position.side not in {"Buy", "Sell"}
                        or not position.size.is_finite()
                        or position.size < 0
                    ):
                        self._difference(result, "UNSUPPORTED_LINEAR_POSITION", position.symbol)
                        continue
                    actual[position.symbol] = actual.get(position.symbol, ZERO) + position.size * (
                        1 if position.side == "Buy" else -1
                    )
                difference = ZERO
                for symbol in set(expected) | set(actual):
                    gap = abs(expected.get(symbol, ZERO) - actual.get(symbol, ZERO))
                    difference += gap
                    if gap > TOLERANCE:
                        self._difference(result, "LINEAR_POSITION_DIFFERENCE", symbol)
                if (
                    account.account_uid != self.account_uid
                    or account.has_debt
                    or account.account_mode.lower() != "unified"
                ):
                    self._difference(result, "LINEAR_ACCOUNT_UNTRUSTED", "account")
                baseline = await session.scalar(
                    select(RuntimeState).where(
                        RuntimeState.environment == self.environment,
                        RuntimeState.key == "spread-cash-baseline",
                    )
                )
                wallet = account.coin("USDT").wallet_balance
                if baseline is None:
                    if pairs or local or fills or any(actual.values()):
                        self._difference(result, "LINEAR_CASH_ANCHOR_MISSING", "USDT")
                    elif wallet is not None and wallet.is_finite():
                        baseline = RuntimeState(
                            environment=self.environment,
                            key="spread-cash-baseline",
                            value={"account_uid": self.account_uid, "wallet": str(wallet)},
                            updated_at=now_utc(),
                        )
                        session.add(baseline)
                if baseline is not None:
                    target = Decimal(baseline.value["wallet"]) + realised - fees
                    if (
                        baseline.value["account_uid"] != self.account_uid
                        or wallet is None
                        or not wallet.is_finite()
                        or abs(target - wallet) > Decimal("0.00000001")
                    ):
                        self._difference(result, "LINEAR_CASH_DIFFERENCE", "USDT")
                for coin, balance in account.coins.items():
                    if coin != "USDT" and balance.wallet_balance != 0:
                        self._difference(result, "NON_USDT_LINEAR_CASH", coin)
                if account.total_equity is None or not account.total_equity.is_finite():
                    self._difference(result, "LINEAR_VALUATION_INCOMPLETE", "account")
                result.allows_resume = not result.has_critical
                previous = await session.scalar(
                    select(ReconciliationRun)
                    .where(
                        ReconciliationRun.environment == self.environment,
                        ReconciliationRun.scope == "spread",
                    )
                    .order_by(ReconciliationRun.id.desc())
                    .limit(1)
                )
                record = (
                    previous is None
                    or result.repaired
                    or previous.differences != [item.as_dict() for item in result.differences]
                    or (started - previous.finished_at).total_seconds()
                    >= self.record_interval_seconds
                )
                if previous is not None:
                    result.run_id = previous.id
                status = "DIFFERENCE" if result.has_critical else "CLEAN"
                logs = [
                    SpreadReconciliationLog(
                        environment=self.environment,
                        checked_at=now_utc(),
                        difference=difference,
                        status=status,
                        detail=result.summary(),
                    )
                ]
                marked = {p.symbol: p.mark_price for p in positions}
                for pair in pairs:
                    facts = pair_facts([o for o in local if o.pair_trade_id == pair.id], stored)
                    a = actual.get(pair.symbol_a, ZERO) * (1 if pair.side_a == "Buy" else -1)
                    b = actual.get(pair.symbol_b, ZERO) * (1 if pair.side_b == "Buy" else -1)
                    qty_a, qty_b = facts.remaining("A"), facts.remaining("B")
                    mark_a, mark_b = (
                        marked.get(pair.symbol_a) or ZERO,
                        marked.get(pair.symbol_b) or ZERO,
                    )
                    # 每张快照只比较本配对两腿，不把不同合约数量混为同一金额。
                    if pair.closed_at is None:
                        logs.append(
                            SpreadReconciliationLog(
                                environment=self.environment,
                                pair_trade_id=pair.id,
                                checked_at=now_utc(),
                                local_qty_a=qty_a,
                                local_qty_b=qty_b,
                                exchange_qty_a=a,
                                exchange_qty_b=b,
                                local_notional_a=qty_a * mark_a,
                                local_notional_b=qty_b * mark_b,
                                exchange_notional_a=a * mark_a,
                                exchange_notional_b=b * mark_b,
                                difference=abs(qty_a - a) + abs(qty_b - b),
                                status=status,
                                detail=result.summary(),
                            )
                        )
                if record:
                    session.add_all(logs)
                await session.commit()
        except Exception as exc:  # noqa: BLE001 -- 任一校准查询失败均阻断，不吞为 clean。
            record = True
            self._difference(result, "LINEAR_CALIBRATION_FAILED", type(exc).__name__)
            async with self.sessionmaker() as session:
                session.add(
                    SpreadReconciliationLog(
                        environment=self.environment,
                        checked_at=now_utc(),
                        status="DIFFERENCE",
                        detail=result.summary(),
                    )
                )
                await session.commit()
        result.allows_resume = not result.has_critical
        if not record:
            return result
        async with self.sessionmaker() as session:
            row = ReconciliationRun(
                environment=self.environment,
                scope="spread",
                started_at=started,
                finished_at=now_utc(),
                differences=[item.as_dict() for item in result.differences],
                repaired=result.repaired,
                allows_resume=result.allows_resume,
            )
            session.add(row)
            await session.commit()
            result.run_id = row.id
        return result

    @staticmethod
    def _difference(result, kind, reference):
        result.differences.append(Difference(kind, kind, "CRITICAL", reference=str(reference)))

    @staticmethod
    def _valid_fill(row, fill):
        return (
            fill.category == "linear"
            and fill.symbol == row.symbol
            and fill.side == row.side
            and (not row.exchange_order_id or row.exchange_order_id == fill.order_id)
            and fill.exec_qty.is_finite()
            and fill.exec_qty > 0
            and fill.exec_price.is_finite()
            and fill.exec_price > 0
            and fill.exec_fee is not None
            and fill.exec_fee.is_finite()
            and fill.exec_fee_asset == "USDT"
            and bool(fill.exec_id)
            and fill.exec_time.tzinfo is not None
        )

    @staticmethod
    def _valid_order(row, view):
        return (
            view.category == "linear"
            and view.symbol == row.symbol
            and view.side == row.side
            and view.qty == row.qty
            and view.price == row.price
            and view.order_type.lower() == row.order_type.lower()
            and view.time_in_force == row.time_in_force
            and view.reduce_only == (row.role == "FLATTEN")
            and view.position_idx in (0, None)
            and (not row.exchange_order_id or row.exchange_order_id == view.order_id)
            and view.qty_filled.is_finite()
            and ZERO <= view.qty_filled <= row.qty
        )
