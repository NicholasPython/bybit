"""Persistent spread orchestration over canonical intents, orders and executions.

Plans commit before submission. A retry reuses its exact signal identity; execution
facts are projected afresh, including partial and late fills after cancellation.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict
from decimal import Decimal

from sqlalchemy import select

from bybitbot.db.models import Execution, Order, TradeIntent
from bybitbot.strategy.base import IntentDraft
from bybitbot.validation import (
    PositionLedger,
    SpreadLeg,
    SpreadProtocol,
    StudyPhase,
    choose_candidate,
    executable_price,
    limit_price,
    remaining_risk_budget,
    spread_economics,
    structure_ok,
)
from bybitbot.validation.journal import append_event
from bybitbot.validation.models import ValidationObservation, ValidationPosition
from bybitbot.validation.sampling import json_value

ZERO = Decimal(0)
TERMINAL_ORDERS = {"FILLED", "CANCELED", "REJECTED", "EXPIRED"}
ACTIONS = {
    "open_long": ("OPTION_BUY_OPEN", "Buy"),
    "open_short": ("OPTION_SELL_OPEN", "Sell"),
    "close_short": ("OPTION_BUY_CLOSE", "Buy"),
    "close_long": ("OPTION_SELL_CLOSE", "Sell"),
}


def millis(moment):
    return int(moment.timestamp() * 1000)


class ValidationOrchestrator:
    def __init__(self, store, *, submit, cancel):
        self.store, self.submit, self.cancel = store, submit, cancel

    async def tick(self, *, now_ms: int, allows_open: bool):
        jobs, cancellations = [], []
        for detached in await self.store.active():
            async with self.store.sessionmaker() as session:
                study = await self.store.locked(session, detached.id)
                positions = list(
                    await session.scalars(
                        select(ValidationPosition)
                        .where(ValidationPosition.study_id == study.id)
                        .order_by(ValidationPosition.base)
                    )
                )
                observation = await session.scalar(
                    select(ValidationObservation)
                    .where(ValidationObservation.study_id == study.id)
                    .order_by(ValidationObservation.slot_ms.desc())
                    .limit(1)
                )
                protocol = SpreadProtocol.from_mapping(study.protocol)
                for position in positions:
                    state = dict(position.state)
                    if not state.get("binding"):
                        continue
                    pending, orders = await self._project(session, study, position, state, now_ms)
                    fresh = (
                        observation is not None
                        and observation.status == "ok"
                        and 0
                        <= now_ms - observation.observed_ms
                        <= protocol.max_response_age_seconds * 1000
                    )
                    sample = (
                        observation.payload.get("bases", {}).get(position.base, {}) if fresh else {}
                    )
                    legs = {
                        leg.symbol: leg
                        for leg in (SpreadLeg.from_mapping(row) for row in sample.get("legs", []))
                    }
                    timed_out = now_ms >= study.end_ms or (
                        state.get("candidate")
                        and now_ms >= state["candidate"]["long_leg"]["expiry_ms"]
                    )
                    if pending:
                        opening = pending["purpose"].startswith("open_")
                        expired = now_ms >= pending["deadline_ms"]
                        cancelled = (
                            timed_out
                            or expired
                            or (opening and (not allows_open or state.get("stop_requested")))
                        )
                        if cancelled:
                            state["exit_reason"] = "study_end" if timed_out else "entry_cancelled"
                            state["abort"] = opening or state.get("abort", False)
                            cancellations.extend(
                                order for order in orders if order.status not in TERMINAL_ORDERS
                            )
                            if not pending.get("intent_exists"):
                                # No intent means submission never crossed the boundary.
                                state.pop("pending", None)
                                pending = None
                        elif not pending.get("intent_exists"):
                            jobs.append((position.id, state["binding"], pending))
                        if pending:
                            position.state = json_value(state)
                            continue
                    if timed_out:
                        position.phase = (
                            "study_ended_unresolved"
                            if state.get("long_qty") != "0" or state.get("short_qty") != "0"
                            else "cancelled"
                            if position.phase == "waiting"
                            else position.phase
                        )
                        position.state = json_value(state)
                        continue
                    if position.phase == "waiting":
                        if not allows_open or not sample.get("signal", {}).get("eligible"):
                            position.state = json_value(state)
                            continue
                        available = remaining_risk_budget(
                            {p.base: p.reserved_risk for p in positions}, protocol
                        )
                        candidate = choose_candidate(
                            base=position.base,
                            legs=list(legs.values()),
                            now_ms=now_ms,
                            available=available,
                            protocol=protocol,
                            direction=study.direction,
                        )
                        if candidate:
                            state["candidate"] = json_value(asdict(candidate))
                            position.reserved_risk = candidate.reserved_risk
                            position.phase = "opening_long"
                            self._plan(
                                position,
                                state,
                                "open_long",
                                candidate.long_leg,
                                candidate.qty,
                                protocol,
                                now_ms,
                            )
                    else:
                        self._advance(
                            study, position, state, legs, sample, protocol, now_ms, allows_open
                        )
                    if state.get("pending"):
                        jobs.append((position.id, state["binding"], state["pending"]))
                    position.state = json_value(state)
                await session.commit()
        # Cancellation confirmation and actual executions are read on the next tick.
        for order in cancellations:
            await self.cancel(order)
        for position_id, binding, pending in jobs:
            await self.submit(position_id, binding, pending, self.draft(position_id, pending))

    async def _project(self, session, study, position, state, now_ms):
        long_qty = short_qty = cash = fees = ZERO
        fees_known = True
        pending, pending_orders = None, []
        for plan in state.get("intents", []):
            intent = await session.scalar(
                select(TradeIntent).where(
                    TradeIntent.environment == study.environment,
                    TradeIntent.account_uid == study.account_uid,
                    TradeIntent.signal_id == plan["signal_id"],
                )
            )
            orders = (
                []
                if intent is None
                else list(await session.scalars(select(Order).where(Order.intent_id == intent.id)))
            )
            fills = (
                []
                if not orders
                else list(
                    await session.scalars(
                        select(Execution)
                        .where(Execution.order_id.in_([order.id for order in orders]))
                        .order_by(Execution.id)
                    )
                )
            )
            for fill in fills:
                fee = fill.exec_fee or ZERO
                fees_known = (
                    fees_known and fill.exec_fee is not None and fill.exec_fee_asset == "USDT"
                )
                cash += (
                    1 if fill.side.lower() == "sell" else -1
                ) * fill.exec_price * fill.exec_qty - fee
                fees += fee
                if plan["purpose"] == "open_long":
                    long_qty += fill.exec_qty
                elif plan["purpose"] == "close_long":
                    long_qty -= fill.exec_qty
                elif plan["purpose"] == "open_short":
                    short_qty += fill.exec_qty
                else:
                    short_qty -= fill.exec_qty
                key = f"execution:{fill.id}"
                source = fill.raw.get("quote_source_ms")
                latency = (
                    None
                    if source is None
                    else str(Decimal(source - plan["decision_source_ms"]) / 1000)
                )
                await append_event(
                    session,
                    study,
                    dedupe_key=key,
                    kind="execution",
                    occurred_ms=millis(fill.exec_time),
                    payload=json_value(
                        {
                            "base": position.base,
                            "purpose": plan["purpose"],
                            "execution_id": fill.id,
                            "signal_id": plan["signal_id"],
                            "qty": fill.exec_qty,
                            "price": fill.exec_price,
                            "fee": fill.exec_fee,
                            "decision_source_ms": plan["decision_source_ms"],
                            "quote_source_ms": source,
                            "latency_seconds": latency,
                        }
                    ),
                )
            done = intent is not None and (
                intent.status == "REJECTED"
                or (
                    orders
                    and all(order.status in TERMINAL_ORDERS for order in orders)
                    and sum((order.qty_filled for order in orders), ZERO)
                    == sum((fill.exec_qty for fill in fills), ZERO)
                )
            )
            if state.get("pending", {}).get("signal_id") == plan["signal_id"]:
                if done:
                    state.pop("pending", None)
                    state["last_purpose"] = plan["purpose"]
                    if plan["purpose"].startswith("open_") and now_ms >= plan["deadline_ms"]:
                        state["abort"] = True
                else:
                    pending = dict(plan, intent_exists=intent is not None)
                    pending_orders = orders
        position.cash, position.fees = cash, fees
        state.update(
            long_qty=str(long_qty.normalize()),
            short_qty=str(short_qty.normalize()),
            fees_known=fees_known,
        )
        if long_qty < 0 or short_qty < 0 or short_qty > long_qty:
            state["accounting_error"] = "inconsistent_execution_exposure"
        if position.phase in {"closed", "cancelled", "aborted"} and (long_qty or short_qty):
            position.phase = "aborting"
            state["abort"] = True
            position.reserved_risk = Decimal(state["candidate"]["reserved_risk"])
        return pending, pending_orders

    def _advance(self, study, position, state, legs, sample, protocol, now_ms, allows_open):
        if state.get("accounting_error") or not state.get("fees_known", True):
            return
        candidate = state.get("candidate")
        if not candidate:
            return
        long_qty, short_qty = Decimal(state["long_qty"]), Decimal(state["short_qty"])
        target = Decimal(candidate["qty"])
        long_leg = legs.get(candidate["long_leg"]["symbol"])
        short_leg = legs.get(candidate["short_leg"]["symbol"])
        if position.phase == "opening_long":
            if (
                long_qty != target
                or state.get("abort")
                or not allows_open
                or state.get("stop_requested")
            ):
                state["abort"] = True
                position.phase = "aborting"
            elif long_leg and short_leg:
                economics = spread_economics(
                    long_leg,
                    short_leg,
                    target,
                    protocol,
                    paid_long=-position.cash,
                    direction=study.direction,
                )
                if structure_ok(
                    long_leg, short_leg, now_ms, protocol, direction=study.direction
                ) and (
                    economics
                    and economics.eligible
                    and economics.stress_debit <= position.reserved_risk
                ):
                    position.phase = "opening_short"
                    self._plan(position, state, "open_short", short_leg, target, protocol, now_ms)
                else:
                    position.phase = "aborting"
                    state["abort"] = True
        elif position.phase == "opening_short":
            if (
                short_qty == long_qty == target
                and not state.get("abort")
                and allows_open
                and not state.get("stop_requested")
            ):
                position.phase = "open"
                state.update(opened_ms=now_ms, entry_debit=str(-position.cash))
            else:
                position.phase = "aborting"
                state["abort"] = True
        if position.phase == "open":
            net = position.cash
            for leg, qty, side, sign in (
                (long_leg, long_qty, "Sell", 1),
                (short_leg, short_qty, "Buy", -1),
            ):
                price = executable_price(leg, side, qty, protocol) if leg else None
                if price is None:
                    net = None
                    break
                net += sign * price * qty - protocol.option_fee(
                    premium=price, qty=qty, index_price=leg.index_price
                )
            domain = PositionLedger(
                base=position.base,
                phase=StudyPhase.OPEN,
                opened_ms=state["opened_ms"],
                entry_debit=Decimal(state["entry_debit"]),
            )
            reason = domain.exit_trigger(
                now_ms=now_ms,
                end_ms=study.end_ms,
                expiry_ms=candidate["long_leg"]["expiry_ms"],
                signal_eligible=sample.get("signal", {}).get("eligible", True),
                net_pnl=net,
                protocol=protocol,
            )
            if reason or state.get("stop_requested"):
                state["exit_reason"] = reason or "manual_stop"
                position.phase = "closing_short"
        if position.phase in {"aborting", "closing_short", "closing_long"}:
            if short_qty:
                if short_leg:
                    self._plan(
                        position, state, "close_short", short_leg, short_qty, protocol, now_ms
                    )
            elif long_qty:
                if long_leg:
                    position.phase = "aborting" if state.get("abort") else "closing_long"
                    self._plan(position, state, "close_long", long_leg, long_qty, protocol, now_ms)
            else:
                position.phase = "aborted" if state.get("abort") else "closed"
                position.reserved_risk = ZERO

    def _plan(self, position, state, purpose, leg, qty, protocol, now_ms):
        if (
            leg.source_ms is None
            or not 0 <= now_ms - leg.source_ms <= protocol.max_response_age_seconds * 1000
        ):
            return
        action, side = ACTIONS[purpose]
        opening = purpose.startswith("open_")
        price = executable_price(leg, side, qty, protocol, entry=opening)
        if price is None:
            return
        boundary = limit_price(leg, side, protocol) if opening else price
        attempt = len(state.get("intents", []))
        signal = f"v:{position.study_id}:{position.base}:{purpose}:{attempt}"
        pending = json_value(
            {
                "signal_id": signal,
                "purpose": purpose,
                "action": action,
                "side": side,
                "leg": asdict(leg),
                "qty": qty,
                "price": price,
                "boundary": boundary,
                "spend_limit": boundary * qty
                + protocol.option_fee(premium=boundary, qty=qty, index_price=leg.index_price)
                if side == "Buy"
                else ZERO,
                "created_ms": now_ms,
                "decision_source_ms": leg.source_ms,
                "deadline_ms": now_ms + protocol.pending_ttl_seconds * 1000,
                "coverage_symbol": state["candidate"]["long_leg"]["symbol"],
            }
        )
        state["intents"] = [*state.get("intents", []), pending]
        state["pending"] = pending

    @staticmethod
    def draft(position_id, pending):
        leg = pending["leg"]
        opening = pending["purpose"].startswith("open_")
        return IntentDraft(
            action=pending["action"],
            category="option",
            symbol=leg["symbol"],
            side=pending["side"],
            qty=Decimal(pending["qty"]),
            qty_unit="contract",
            currency="USDT",
            spend_limit=Decimal(pending["spend_limit"]),
            price_limit=Decimal(pending["boundary"]),
            reduce_qty=None if opening else Decimal(pending["qty"]),
            underlying=leg["base"],
            delivery_time=dt.datetime.fromtimestamp(leg["expiry_ms"] / 1000, dt.UTC),
            strike_price=Decimal(leg["strike"]),
            options_type=leg["option_type"],
            ttl_seconds=max(1, (pending["deadline_ms"] - pending["created_ms"]) // 1000),
            rationale={
                "validation_position_id": position_id,
                "decision_source_ms": pending["decision_source_ms"],
            },
            validation_position_id=position_id,
            coverage_symbol=pending["coverage_symbol"],
        )
