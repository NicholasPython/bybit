"""业务账本写入（LED-01 / LED-04）。

需求原文：
    保存交易意图、风险决策、订单、成交、费用、资金流、期权结算和配置历史；
    业务账本追加事件，纠错使用更正事件。以账户/环境/产品与成交编号构成去重域，
    费用保留原币种和估值信息。

    现货按策略/币种、期权按策略/合约分别采用移动加权成本；部分平仓按数量比例
    结转成本和已分配开仓费用，再计当次平仓费用。不同策略成本不混合；
    外部导入记录成本来源，未知时不虚构历史已实现收益。到期结算独立记账。

三条规则：
    * **只追加**：纠错写更正事件（``correction_of_id``），不原地改写历史。
    * **去重域**：环境 + 产品 + 交易所成交编号（``uq_execution_dedupe``）。
    * **费用不混算**：费用保留原币种，另记估值信息。
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bybitbot.clock import now_utc
from bybitbot.db.models import (
    CashFlow,
    Execution,
    Instrument,
    Inventory,
    LedgerEvent,
    Order,
    Position,
)
from bybitbot.money import dec


class LedgerWriter:
    """账本写入器。所有方法都要求调用方已经处于事务中。"""

    def __init__(self, environment: str) -> None:
        self.environment = environment

    # ------------------------------------------------------------------
    # 通用事件
    # ------------------------------------------------------------------

    async def append_event(
        self,
        session: AsyncSession,
        *,
        event_type: str,
        dedupe_key: str,
        payload: dict[str, object],
        occurred_at: dt.datetime | None = None,
        correction_of_id: int | None = None,
    ) -> LedgerEvent | None:
        """追加一条账本事件。重复的 dedupe_key 返回 ``None``（幂等）。"""
        event = LedgerEvent(
            environment=self.environment,
            category=str(payload.get("category", "")),
            event_type=event_type,
            dedupe_key=dedupe_key,
            payload=payload,
            correction_of_id=correction_of_id,
            occurred_at=occurred_at or now_utc(),
        )
        try:
            # 用保存点包住插入：唯一约束冲突时只回滚这一条，
            # 不能连带回滚调用方在同一事务里已经做完的事。
            async with session.begin_nested():
                session.add(event)
                await session.flush()
        except IntegrityError:
            return None
        return event

    # ------------------------------------------------------------------
    # 成交
    # ------------------------------------------------------------------

    async def record_execution(
        self,
        session: AsyncSession,
        *,
        exchange_exec_id: str,
        category: str,
        exchange_order_id: str | None,
        order_link_id: str | None,
        order_id: int | None,
        symbol: str,
        side: str,
        exec_price: Decimal,
        exec_qty: Decimal,
        exec_fee: Decimal | None,
        exec_fee_asset: str | None,
        is_maker: bool | None,
        exec_time: dt.datetime,
        raw: dict[str, object] | None = None,
    ) -> Execution | None:
        """写入一笔成交。重复成交编号返回 ``None``（EXE-03 去重）。"""
        execution = Execution(
            environment=self.environment,
            category=category,
            exchange_exec_id=exchange_exec_id,
            exchange_order_id=exchange_order_id,
            order_link_id=order_link_id,
            order_id=order_id,
            symbol=symbol,
            side=side.lower(),
            exec_price=dec(exec_price),
            exec_qty=dec(exec_qty),
            exec_fee=dec(exec_fee) if exec_fee is not None else None,
            exec_fee_asset=exec_fee_asset.upper() if exec_fee_asset else None,
            is_maker=is_maker,
            exec_time=exec_time,
            raw=raw or {},
        )
        try:
            async with session.begin_nested():
                session.add(execution)
                await session.flush()
        except IntegrityError:
            return None
        return execution

    # ------------------------------------------------------------------
    # 策略归属库存与成本
    # ------------------------------------------------------------------

    async def apply_fill_to_inventory(
        self,
        session: AsyncSession,
        *,
        strategy_id: int | None,
        symbol: str,
        side: str,
        qty: Decimal,
        price: Decimal,
        fee: Decimal = Decimal("0"),
        fee_asset: str | None = None,
        currency: str = "USDT",
        category: str = "spot",
        base_asset: str | None = None,
        fee_value: Decimal | None = None,
    ) -> tuple[Decimal, Decimal]:
        """把一笔成交计入策略归属库存。

        Returns:
            ``(库存变化, 本次已实现盈亏)``。

        成本口径（LED-04）：
            * 买入开仓：移动加权成本 = (原成本 + 本次成本 + 计入成本的费用) / 新数量；
              基础币手续费直接增加成本。
            * 卖出平仓：按数量比例结转成本，卖出价与结转成本的差额为已实现盈亏，
              再扣本次平仓费用。不同策略成本不混合。
        """
        qty = dec(qty)
        price = dec(price)
        fee = dec(fee)
        if strategy_id is None:
            return Decimal("0"), Decimal("0")
        if (
            side.lower() not in {"buy", "sell"}
            or not qty.is_finite()
            or not price.is_finite()
            or not fee.is_finite()
            or qty <= 0
            or price <= 0
        ):
            raise ValueError("无效成交方向、数量或价格")
        currency = currency.upper()
        asset = fee_asset.upper() if fee_asset else currency
        if base_asset is None and category == "spot" and symbol.endswith(currency):
            base_asset = symbol[: -len(currency)]
        base_fee = fee if category == "spot" and asset == base_asset else Decimal("0")
        quote_fee = (
            fee if asset == currency else (dec(fee_value) if fee_value is not None else None)
        )
        unvalued_fee = fee != 0 and base_fee == 0 and quote_fee is None
        # 串行化同策略库存的新建/更新，包括尚不存在的行。
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": f"{self.environment}:cost:{strategy_id}:{symbol}"},
        )
        inventory = await session.scalar(
            select(Inventory)
            .where(
                Inventory.environment == self.environment,
                Inventory.strategy_id == strategy_id,
                Inventory.symbol == symbol,
            )
            .with_for_update()
        )
        old_qty = inventory.qty if inventory is not None else Decimal("0")
        old_cost = inventory.avg_cost if inventory is not None else Decimal("0")
        known_cost = inventory is None or inventory.cost_source not in {
            None,
            "unknown",
            "unvalued_fee",
            "overdrawn",
        }
        if side.lower() == "buy":
            net_qty = qty - base_fee
            if net_qty <= 0:
                raise ValueError("基础币手续费不能耗尽全部买入数量")
            cost_increment = price * qty + (quote_fee or Decimal("0"))
            new_qty = old_qty + net_qty
            new_avg = (old_cost * old_qty + cost_increment) / new_qty
            if inventory is None:
                inventory = Inventory(
                    environment=self.environment,
                    strategy_id=strategy_id,
                    symbol=symbol,
                    currency=currency,
                    trusted=True,
                    cost_source="bot_fill",
                )
                session.add(inventory)
            inventory.qty = new_qty
            inventory.avg_cost = new_avg
            if unvalued_fee or not known_cost:
                inventory.cost_source = "unvalued_fee" if unvalued_fee else "unknown"
                inventory.trusted = False
            await session.flush()
            return net_qty, Decimal("0")

        # 基础币卖出手续费额外消耗库存，不能以计价币数额直接相加。
        removed = qty + base_fee
        closed = min(removed, old_qty)
        proceeds = price * qty
        realised = proceeds - old_cost * closed - (quote_fee or Decimal("0"))
        if inventory is not None:
            inventory.qty = old_qty - closed
            if inventory.qty == 0:
                inventory.avg_cost = Decimal("0")
            if removed > old_qty:
                inventory.trusted = False
                inventory.cost_source = "overdrawn"
            elif unvalued_fee:
                inventory.trusted = False
                inventory.cost_source = "unvalued_fee"
            await session.flush()
        if not known_cost or removed > old_qty or unvalued_fee:
            # 原始费用和成交仍入账，但缺少成本/汇率时不能虚构已实现收益。
            realised = Decimal("0")
        return -closed, realised

    # ------------------------------------------------------------------
    # 外部资金流
    # ------------------------------------------------------------------

    async def record_cash_flow(
        self,
        session: AsyncSession,
        *,
        asset: str,
        amount: Decimal,
        kind: str,
        dedupe_key: str,
        occurred_at: dt.datetime | None = None,
        external: bool = True,
    ) -> CashFlow | None:
        """记录外部资金流（充值 / 提现 / 划转）。

        外部变化计入账户总风险，但不自动归属某策略（LED-03）。
        """
        flow = CashFlow(
            environment=self.environment,
            asset=asset.upper(),
            amount=dec(amount),
            kind=kind,
            external=external,
            dedupe_key=dedupe_key,
            occurred_at=occurred_at or now_utc(),
        )
        try:
            async with session.begin_nested():
                session.add(flow)
                await session.flush()
        except IntegrityError:
            return None
        return flow

    async def record_option_settlement(
        self,
        session: AsyncSession,
        *,
        settlement_id: str,
        symbol: str,
        currency: str,
        occurred_at: dt.datetime,
        allocations: list[dict[str, object]],
        evidence: dict[str, object],
    ) -> LedgerEvent | None:
        """按已核对交割事实释放成本；到时/仓位消失本身不构成交割证据。"""
        if (
            not settlement_id
            or len(settlement_id) > 128
            or occurred_at.tzinfo is None
            or evidence.get("source") not in {"paper", "official_fixture", "bybit"}
            or not evidence.get("reference")
            or evidence.get("balance_reconciled") is not True
            or evidence.get("positions_reconciled") is not True
            or not allocations
        ):
            raise ValueError("交割缺少完整余额、持仓及来源核对证据")
        if self.environment != "paper" and evidence["source"] != "bybit":
            raise ValueError("模拟/样例交割不能用于真实账户")
        currency = currency.upper()
        key = f"settlement:{settlement_id}"
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": f"{self.environment}:{key}"},
        )
        existing = await session.scalar(
            select(LedgerEvent).where(
                LedgerEvent.environment == self.environment,
                LedgerEvent.dedupe_key == key,
            )
        )
        normalized = []
        for item in allocations:
            strategy_id = int(item["strategy_id"])
            qty, payout, fee = (dec(item[field]) for field in ("qty", "payout", "fee"))
            if (
                strategy_id <= 0
                or any(not value.is_finite() for value in (qty, payout, fee))
                or qty <= 0
                or payout < 0
                or fee < 0
            ):
                raise ValueError("交割分配数量、支付和费用无效")
            normalized.append(
                {
                    "strategy_id": strategy_id,
                    "qty": str(qty),
                    "payout": str(payout),
                    "fee": str(fee),
                }
            )
        normalized.sort(key=lambda item: item["strategy_id"])
        if len({item["strategy_id"] for item in normalized}) != len(normalized):
            raise ValueError("交割分配不能重复策略")
        fact = {
            "symbol": symbol,
            "currency": currency,
            "occurred_at": occurred_at.isoformat(),
            "allocations": normalized,
        }
        if existing is not None:
            if any(
                (
                    existing.payload.get("input_allocations")
                    if name == "allocations"
                    else existing.payload.get(name)
                )
                != value
                for name, value in fact.items()
            ):
                raise ValueError("同一交割编号的事实冲突")
            return None
        spec = await session.scalar(
            select(Instrument).where(
                Instrument.environment == self.environment,
                Instrument.category == "option",
                Instrument.symbol == symbol,
            )
        )
        if (
            spec is None
            or spec.settle_coin != currency
            or spec.delivery_time is None
            or occurred_at < spec.delivery_time
        ):
            raise ValueError("交割合约、结算币或到期时间未验证")
        pending = await session.scalar(
            select(Order.id)
            .where(
                Order.environment == self.environment,
                Order.symbol == symbol,
                (Order.status.notin_(["FILLED", "CANCELED", "REJECTED", "EXPIRED"]))
                | (Order.sync_state != "CONFIRMED"),
            )
            .limit(1)
        )
        if pending is not None:
            raise ValueError("存在活动/未知订单，交割尚不能终结预算")
        entries = []
        total_net = Decimal("0")
        for item in normalized:
            strategy_id = item["strategy_id"]
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                {"key": f"{self.environment}:cost:{strategy_id}:{symbol}"},
            )
            inventory = await session.scalar(
                select(Inventory)
                .where(
                    Inventory.environment == self.environment,
                    Inventory.strategy_id == strategy_id,
                    Inventory.symbol == symbol,
                )
                .with_for_update()
            )
            qty, payout, fee = (dec(item[field]) for field in ("qty", "payout", "fee"))
            if inventory is None or inventory.qty < qty or inventory.currency != currency:
                raise ValueError("交割分配超过策略已有归属")
            cost = inventory.avg_cost * qty
            inventory.qty -= qty
            if inventory.qty == 0:
                inventory.avg_cost = Decimal("0")
            known = inventory.cost_source not in {None, "unknown", "unvalued_fee", "overdrawn"}
            entries.append(
                {
                    **item,
                    "released_cost": str(cost),
                    "realised_pnl": str(payout - fee - cost) if known else None,
                }
            )
            total_net += payout - fee
        payload = {
            "category": "option",
            **fact,
            "allocations": entries,
            "evidence": evidence,
            "net_cash": str(total_net),
        }
        # 幂等比较只使用原始分配事实，成本按原始归属计算并作为附加证据保存。
        payload["input_allocations"] = normalized
        event = await self.append_event(
            session,
            event_type="SETTLEMENT",
            dedupe_key=key,
            payload=payload,
            occurred_at=occurred_at,
        )
        if event is None:
            raise RuntimeError("交割事件并发冲突，应回滚当前事务")
        await self.record_cash_flow(
            session,
            asset=currency,
            amount=total_net,
            kind="OPTION_SETTLEMENT",
            dedupe_key=key,
            occurred_at=occurred_at,
            external=False,
        )
        await session.flush()
        return event

    # ------------------------------------------------------------------
    # 账户实际持仓
    # ------------------------------------------------------------------

    async def upsert_position(
        self,
        session: AsyncSession,
        *,
        category: str,
        symbol: str,
        position_idx: int,
        side: str | None,
        size: Decimal,
        avg_price: Decimal | None = None,
        mark_price: Decimal | None = None,
        settle_coin: str | None = None,
        delivery_time: dt.datetime | None = None,
        delta: Decimal | None = None,
        gamma: Decimal | None = None,
        vega: Decimal | None = None,
        theta: Decimal | None = None,
        unrealised_pnl: Decimal | None = None,
        trusted: bool = True,
    ) -> Position:
        """写入账户实际持仓快照。"""
        stmt = Position.__table__.select().where(
            Position.environment == self.environment,
            Position.category == category,
            Position.symbol == symbol,
            Position.position_idx == position_idx,
        )
        row = (await session.execute(stmt)).mappings().first()
        if row:
            position = await session.get(Position, row["id"])
        else:
            position = Position(
                environment=self.environment,
                category=category,
                symbol=symbol,
                position_idx=position_idx,
            )
            session.add(position)
        position.side = side
        position.size = dec(size)
        position.avg_price = dec(avg_price) if avg_price is not None else position.avg_price
        position.mark_price = dec(mark_price) if mark_price is not None else position.mark_price
        position.settle_coin = settle_coin or position.settle_coin
        position.delivery_time = delivery_time or position.delivery_time
        position.delta = dec(delta) if delta is not None else position.delta
        position.gamma = dec(gamma) if gamma is not None else position.gamma
        position.vega = dec(vega) if vega is not None else position.vega
        position.theta = dec(theta) if theta is not None else position.theta
        position.unrealised_pnl = (
            dec(unrealised_pnl) if unrealised_pnl is not None else position.unrealised_pnl
        )
        position.trusted = trusted
        await session.flush()
        return position


def order_execution_dedupe_key(exchange_exec_id: str) -> str:
    """成交去重键（LED-01 的去重域）。"""
    return f"exec:{exchange_exec_id}"


def order_link_id_for(intent_id: int, rebuild_index: int) -> str:
    """新物理订单的全局唯一编号，保存到 Order 后只重用持久化值。

    数据库本地整数会在其他环境、全新数据库及恢复后复用；UUID 避免这些编号
    冲突。重复提交通过意图锁和已有 Order 记录去重，不能重新生成并发送。
    """
    if intent_id <= 0 or rebuild_index < 0:
        raise ValueError("无效意图编号或重建次数")
    return f"bb{uuid.uuid4().hex}"


__all__ = [
    "LedgerWriter",
    "order_execution_dedupe_key",
    "order_link_id_for",
]
