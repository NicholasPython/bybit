"""Paper spread authority derived from canonical fills and durable pending claims.

JSON phase/quantity projections never authorize exposure. The account authorization
lock is shared by submission and fill application, including unsent reservations.
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select, text

from bybitbot.db.models import (
    Execution,
    Instrument,
    Inventory,
    Order,
    Reservation,
    Strategy,
    StrategyConfigVersion,
    TradeIntent,
)
from bybitbot.risk.reservation import ReservationError
from bybitbot.validation.models import ValidationPosition, ValidationStudy

ZERO = Decimal(0)
TERMINAL = {"FILLED", "CANCELED", "REJECTED", "EXPIRED"}
SHORT_ACTIONS = {"OPTION_SELL_OPEN", "OPTION_BUY_CLOSE"}
SIDES = {
    "OPTION_BUY_OPEN": "buy", "OPTION_SELL_OPEN": "sell",
    "OPTION_BUY_CLOSE": "buy", "OPTION_SELL_CLOSE": "sell",
}


async def lock_authorization(session, environment):
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
        {"key": f"{environment}:account-authorization"},
    )


def reject(detail):
    raise ReservationError("SPREAD_AUTHORITY", detail)


async def facts(session, environment, account_uid):
    """Return (intent, actual filled qty, still-authorized qty, uncertain) rows."""
    intents = list(await session.scalars(select(TradeIntent).where(
        TradeIntent.environment == environment, TradeIntent.account_uid == account_uid,
        TradeIntent.category == "option",
    )))
    ids = [i.id for i in intents]
    orders = list(await session.scalars(select(Order).where(Order.intent_id.in_(ids))))
    executions = (await session.execute(
        select(Execution.order_id, Execution.exec_qty)
        .where(Execution.order_id.in_([o.id for o in orders]))
    )).all()
    fills = {}
    for order_id, qty in executions:
        fills[order_id] = fills.get(order_id, ZERO) + qty
    reservations = list(await session.scalars(select(Reservation).where(
        Reservation.intent_id.in_(ids), Reservation.status.in_(["HELD", "PARTIALLY_CONSUMED"]),
    )))
    held = {r.intent_id for r in reservations if r.amount > r.consumed + r.released}
    result = []
    for intent in intents:
        owned = [o for o in orders if o.intent_id == intent.id]
        qty = sum((fills.get(o.id, ZERO) for o in owned), ZERO)
        uncertain = any(o.sync_state not in {"NONE", "CONFIRMED"}
                        or o.qty_filled != fills.get(o.id, ZERO) for o in owned)
        active = intent.id in held or uncertain or any(o.status not in TERMINAL for o in owned)
        pending = max(ZERO, intent.qty - qty) if active else ZERO
        result.append((intent, qty, pending, uncertain))
    return result


async def authorize(session, intent, *, qty=None):
    """Validate scope and remaining capacity; caller holds this lock until commit."""
    await lock_authorization(session, intent.environment)
    qty = intent.qty if qty is None else qty
    scoped = intent.validation_position_id is not None
    if not scoped and intent.action in SHORT_ACTIONS:
        reject("开空/平空必须绑定验证持仓")
    if not scoped and intent.coverage_symbol:
        reject("覆盖标的缺少验证持仓")
    if scoped and intent.environment != "paper":
        reject("验证价差只允许 paper")
    if intent.category != "option":
        if scoped:
            reject("验证价差必须为期权")
        return
    rows = await facts(session, intent.environment, intent.account_uid)
    # No entry path may net an already-owned opposing physical option position.
    if intent.action in {"OPTION_BUY_OPEN", "OPTION_SELL_OPEN"}:
        opposite = "Sell" if intent.action == "OPTION_BUY_OPEN" else "Buy"
        conflict = await session.scalar(select(Inventory.id).where(
            Inventory.environment == intent.environment, Inventory.symbol == intent.symbol,
            Inventory.qty > 0, Inventory.position_side == opposite,
        ).limit(1))
        if conflict:
            reject("同合约已有相反方向归属，禁止隐式净额")
        opposing = "OPTION_SELL_OPEN" if opposite == "Sell" else "OPTION_BUY_OPEN"
        if any(i.id != intent.id and i.symbol == intent.symbol and i.action == opposing
               and (p > 0 or u) for i, _, p, u in rows):
            reject("同合约存在相反方向未完成开仓")
    if not scoped:
        if intent.action == "OPTION_SELL_CLOSE":
            groups = {i.validation_position_id for i, _, _, _ in rows
                      if i.coverage_symbol == intent.symbol and i.validation_position_id}
            for group in groups:
                short = sum((q * (1 if i.action == "OPTION_SELL_OPEN" else -1)
                             for i, q, _, _ in rows if i.validation_position_id == group
                             and i.action in SHORT_ACTIONS), ZERO)
                if short or any((p > 0 or u) for i, _, p, u in rows
                                if i.validation_position_id == group and i.action in SHORT_ACTIONS):
                    reject("覆盖多头仍承担实际或未决空头义务")
        return
    position = await session.get(ValidationPosition, intent.validation_position_id)
    study = await session.get(ValidationStudy, position.study_id) if position else None
    strategy = await session.get(Strategy, intent.strategy_id) if intent.strategy_id else None
    version = await session.scalar(select(StrategyConfigVersion).where(
        StrategyConfigVersion.strategy_id == intent.strategy_id,
        StrategyConfigVersion.version == intent.strategy_version,
    ))
    if (
        not study
        or study.environment != intent.environment
        or study.account_uid != intent.account_uid
        or not strategy or strategy.environment != intent.environment
        or strategy.kind != "validation_spread" or not version
        or position.base not in version.symbols or version.currency != intent.currency):
        reject("验证持仓、账户、策略或不可变版本不匹配")
    binding = position.state.get("binding", {})
    if binding != {"strategy_id": intent.strategy_id, "version": intent.strategy_version}:
        reject("验证持仓绑定不匹配")
    if intent.action not in SIDES or intent.side.lower() != SIDES[intent.action]:
        reject("开平仓目的与方向不匹配")
    if (intent.currency != "USDT" or intent.qty_unit != "contract"
        or not intent.coverage_symbol or not qty.is_finite() or qty <= 0):
        reject("验证期权币种、单位、数量或覆盖标的无效")
    opening = intent.action in {"OPTION_BUY_OPEN", "OPTION_SELL_OPEN"}
    if opening and (study.status != "running" or not strategy.enabled):
        reject("验证任务或策略已停止开仓")
    spec = await session.scalar(select(Instrument).where(
        Instrument.environment == intent.environment, Instrument.category == "option",
        Instrument.symbol == intent.symbol,
    ))
    long = await session.scalar(select(Instrument).where(
        Instrument.environment == intent.environment, Instrument.category == "option",
        Instrument.symbol == intent.coverage_symbol,
    ))
    if (not spec or not long or spec.base_coin != position.base or long.base_coin != position.base
        or spec.settle_coin != "USDT" or long.settle_coin != "USDT"
        or spec.quote_coin != "USDT" or long.quote_coin != "USDT"
        or spec.delivery_time != long.delivery_time or spec.options_type != long.options_type
        or spec.options_type != study.direction or spec.delivery_time != intent.delivery_time
        or spec.strike_price != intent.strike_price or spec.options_type != intent.options_type
        or intent.underlying != position.base):
        reject("覆盖合约的标的、到期、类型、币种或意图规格不一致")
    if intent.action in SHORT_ACTIONS:
        if (not spec.strike_price or not long.strike_price or spec.symbol == long.symbol
            or (study.direction == "Call" and long.strike_price >= spec.strike_price)
            or (study.direction == "Put" and long.strike_price <= spec.strike_price)):
            reject("覆盖行权价方向无效")
    elif spec.symbol != long.symbol:
        reject("多头目的必须对应覆盖合约")
    scoped_rows = [(i, q, ZERO if i.id == intent.id else p, u)
                   for i, q, p, u in rows if i.validation_position_id == position.id]
    if any(i.coverage_symbol != intent.coverage_symbol
           or i.strategy_id != intent.strategy_id or i.strategy_version != intent.strategy_version
           for i, q, p, u in scoped_rows if q or p or u):
        reject("已存在的覆盖范围或归属不匹配")
    long_qty = sum((q * (1 if i.action == "OPTION_BUY_OPEN" else -1)
                    for i, q, _, _ in scoped_rows
                    if i.action in {"OPTION_BUY_OPEN", "OPTION_SELL_CLOSE"}), ZERO)
    short_qty = sum((q * (1 if i.action == "OPTION_SELL_OPEN" else -1)
                     for i, q, _, _ in scoped_rows if i.action in SHORT_ACTIONS), ZERO)
    pending = {action: sum((p for i, _, p, _ in scoped_rows if i.action == action), ZERO)
               for action in SIDES}
    if long_qty < 0 or short_qty < 0 or short_qty > long_qty:
        reject("实际成交敞口不一致")
    if any(u for i, _, _, u in scoped_rows if i.id != intent.id):
        reject("同范围仍有未核实订单或成交")
    if intent.action == "OPTION_SELL_OPEN":
        if qty > long_qty - short_qty - pending["OPTION_SELL_OPEN"] - pending["OPTION_SELL_CLOSE"]:
            reject("已成交覆盖多头不足或已被占用")
    elif intent.action == "OPTION_BUY_CLOSE":
        if qty > short_qty - pending["OPTION_BUY_CLOSE"]:
            reject("实际空头不足或已被其他回补占用")
    elif intent.action == "OPTION_SELL_CLOSE":
        if short_qty or pending["OPTION_SELL_OPEN"] or pending["OPTION_BUY_CLOSE"]:
            reject("先平空并确认全部成交后才能释放覆盖多头")
        if qty > long_qty - pending["OPTION_SELL_CLOSE"]:
            reject("实际多头不足或已有平仓占用")


async def covered_paper_account(session, *, environment, account_uid, positions):
    """Only exactly reconciled canonical short facts may bypass long-only debt guard."""
    shorts = [p for p in positions if p.side == "Sell" and p.size > 0]
    if not shorts:
        return True
    if environment != "paper":
        return False
    rows = await facts(session, environment, account_uid)
    for position in shorts:
        matching = [(i, q) for i, q, _, _ in rows if i.symbol == position.symbol
                    and i.validation_position_id and i.action in SHORT_ACTIONS]
        if sum((q * (1 if i.action == "OPTION_SELL_OPEN" else -1)
                for i, q in matching), ZERO) != position.size:
            return False
        for scope in {i.validation_position_id for i, _ in matching}:
            scoped = [(i, q) for i, q, _, _ in rows if i.validation_position_id == scope]
            short_qty = sum((q * (1 if i.action == "OPTION_SELL_OPEN" else -1)
                             for i, q in scoped if i.action in SHORT_ACTIONS), ZERO)
            long_qty = sum((q * (1 if i.action == "OPTION_BUY_OPEN" else -1)
                            for i, q in scoped
                            if i.action in {"OPTION_BUY_OPEN", "OPTION_SELL_CLOSE"}
                        ),
                        ZERO,
                    )
            coverage = {i.coverage_symbol for i, q in scoped if q}
            if len(coverage) != 1 or short_qty < 0 or short_qty > long_qty:
                return False
            symbol = next(iter(coverage))
            actual = sum(
                (p.size for p in positions if p.symbol == symbol and p.side == "Buy"), ZERO
            )
            inventory = list(await session.scalars(select(Inventory).where(
                Inventory.environment == environment,
                Inventory.symbol.in_([symbol, position.symbol]), Inventory.qty > 0,
            )))
            if actual < long_qty or not inventory or any(not item.trusted for item in inventory):
                return False
    return True
