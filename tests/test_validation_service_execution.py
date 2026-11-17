"""阶段 3：验证价差在真实服务链路里的四腿执行。

前面的编排器用例用桩替代了提交与撮合；这里让真实链路跑一遍：

* 买腿成交 → **开空（``OPTION_SELL_OPEN``）** → **回补空（``OPTION_BUY_CLOSE``）** → 平多；
* 开空占用的是覆盖多头的库存，回补空占用现金；
* 空头腿在账本里是 ``Sell`` 方向的库存，估值时是负债而不是「估值缺失」。

两条腿的行情取自冻结的研究运行（``research/2026-11-15-bull-call-forward``
快照 ``market-4o2c_dfk`` 的真实 ETH Call 报价），只有到期日平移到测试当天
+21 天，好让入场窗口（10–40 天）恒成立。

所有断言都落在持久化事实（意图、风控决策、订单、成交、库存）上。
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import asdict
from decimal import Decimal as D

import pytest
from sqlalchemy import select

from bybitbot.adapters.base import InstrumentSpec, OrderBook, Ticker
from bybitbot.adapters.paper import PaperAdapter
from bybitbot.clock import now_utc
from bybitbot.db.models import (
    Execution,
    Inventory,
    Order,
    Reservation,
    RiskDecision,
    TradeIntent,
)
from bybitbot.ops.control import CommandKind
from bybitbot.service import TradingService
from bybitbot.settings.loader import load_config
from bybitbot.settings.models import DecimalAmount, StrategyConfig
from bybitbot.validation import SpreadLeg, SpreadProtocol
from bybitbot.validation.models import ValidationPosition, ValidationStudy
from bybitbot.validation.sampling import ValidationStore, json_value

#: 冻结研究运行里的真实 ETH Call 行情（买一/卖一/Delta）。
LONG_BID, LONG_ASK, LONG_DELTA = D("103.5"), D("104.4"), D("0.57535214")
SHORT_BID, SHORT_ASK, SHORT_DELTA = D("55.5"), D("55.6"), D("0.39317776")
#: 同一快照里的标的指数价。
INDEX = D("1824.88448896")
LONG_STRIKE = D(1800)
SHORT_STRIKE = D(1900)
#: 研究快照里的最小变动价位与数量步长。
TICK = D("0.1")
QTY_STEP = D("0.1")
MIN_QTY = D("0.1")
MAX_QTY = D(5000)
MIN_PRICE = D("0.1")
MAX_PRICE = D(41000)

#: 协议口径下的入场数量（由 choose_candidate 按 cohort 预算算出）。
EXPECTED_QTY = D("0.4")


def expiry_time() -> dt.datetime:
    """测试当天 +21 天：协议的目标到期距离（21 天）落在 10–40 天窗口内。

    去掉时分秒：意图里的到期时刻必须逐位等于合约目录里的到期时刻，
    取秒以下的差值会让「同一张合约」变成两个不同的时间戳。
    """
    moment = now_utc().replace(hour=0, minute=0, second=0, microsecond=0)
    return moment + dt.timedelta(days=21)


def option_symbol(strike: int, expiry: dt.datetime) -> str:
    return f"ETH-{expiry:%d%b%y}-{strike}-C".upper()


class SpreadMarket(PaperAdapter):
    """两腿价差行情：多头腿与空头腿各有双边报价与 Greeks。

    纸面按挂单价成交、限价只决定是否穿越，所以这里的盘口必须让
    买腿挂在卖一之上、卖腿挂在买一之下，两腿都能成交。
    """

    def __init__(self, expiry: dt.datetime):
        super().__init__(initial_balances={"USDT": D("1000")})
        self.long_symbol = option_symbol(int(LONG_STRIKE), expiry)
        self.short_symbol = option_symbol(int(SHORT_STRIKE), expiry)
        #: 置为 (bid, ask) 后，空头腿的行情换成这对报价（用于测试挂单等不到成交）。
        self.short_book_override: tuple[D, D] | None = None
        for symbol, strike in (
            (self.long_symbol, LONG_STRIKE),
            (self.short_symbol, SHORT_STRIKE),
        ):
            self.set_instrument(
                InstrumentSpec(
                    "option",
                    symbol,
                    "ETH",
                    "USDT",
                    "Trading",
                    tick_size=TICK,
                    qty_step=QTY_STEP,
                    min_order_qty=MIN_QTY,
                    max_order_qty=MAX_QTY,
                    settle_coin="USDT",
                    options_type="Call",
                    strike_price=strike,
                    delivery_time=expiry,
                )
            )

    def _ticker(self, symbol, bid, ask, delta):
        moment = now_utc()
        return Ticker(
            "option",
            symbol,
            bid_price=bid,
            ask_price=ask,
            bid_size=D("55.5"),
            ask_size=D("20"),
            mark_price=(bid + ask) / 2,
            open_interest=D("50"),
            delta=delta,
            iv=D("0.6"),
            index_price=INDEX,
            source_ts=moment,
            received_at=moment,
        )

    async def get_ticker(self, category, symbol):
        if (
            category == "option"
            and symbol != self.long_symbol
            and self.short_book_override is not None
        ):
            bid, ask = self.short_book_override
            return self._ticker(symbol, bid, ask, SHORT_DELTA)
        if category != "option":
            return await super().get_ticker(category, symbol)
        if symbol == self.long_symbol:
            return self._ticker(symbol, LONG_BID, LONG_ASK, LONG_DELTA)
        return self._ticker(symbol, SHORT_BID, SHORT_ASK, SHORT_DELTA)

    async def get_tickers(self, category, *, base_coin=None):
        if category != "option":
            return []
        return [
            await self.get_ticker(category, symbol)
            for symbol in (self.long_symbol, self.short_symbol)
        ]

    async def get_orderbook(self, category, symbol, limit=50):
        if category != "option":
            return await super().get_orderbook(category, symbol, limit)
        moment = now_utc()
        if symbol == self.long_symbol:
            bid, ask = LONG_BID, LONG_ASK
        elif self.short_book_override is not None:
            bid, ask = self.short_book_override
        else:
            bid, ask = SHORT_BID, SHORT_ASK
        return OrderBook(
            "option",
            symbol,
            ((bid, D("55.5")),),
            ((ask, D("20")),),
            source_ts=moment,
            received_at=moment,
        )


def spread_legs(expiry: dt.datetime, source_ms: int) -> list[SpreadLeg]:
    """冻结研究运行里的两腿 Call（到期日平移，行情照搬）。"""
    shared = {
        "base": "ETH",
        "status": "Trading",
        "option_type": "Call",
        "expiry_ms": int(expiry.timestamp() * 1000),
        "index_price": INDEX,
        "bid_size": D("55.5"),
        "ask_size": D("20"),
        "tick_size": TICK,
        "qty_step": QTY_STEP,
        "min_qty": MIN_QTY,
        "max_qty": MAX_QTY,
        "min_price": MIN_PRICE,
        "max_price": MAX_PRICE,
        "source_ms": source_ms,
    }
    return [
        SpreadLeg(
            symbol=option_symbol(int(LONG_STRIKE), expiry),
            strike=LONG_STRIKE,
            bid=LONG_BID,
            ask=LONG_ASK,
            delta=LONG_DELTA,
            **shared,
        ),
        SpreadLeg(
            symbol=option_symbol(int(SHORT_STRIKE), expiry),
            strike=SHORT_STRIKE,
            bid=SHORT_BID,
            ask=SHORT_ASK,
            delta=SHORT_DELTA,
            **shared,
        ),
    ]


def spread_config():
    """只保留一个验证价差策略；禁开仓窗口在测试里放开，避免整点依赖。"""
    config = load_config("config/config.example.yaml")
    strategy = StrategyConfig(
        name="validation-spread",
        kind="validation_spread",
        enabled=True,
        symbols=("ETH",),
        schedule_utc=(),
        signal_ttl_seconds=60,
        cooldown_seconds=0,
        budget_per_run=DecimalAmount(value=D("100"), currency="USDT"),
        max_spend_per_trade=DecimalAmount(value=D("100"), currency="USDT"),
        exit_rules=(),
    )
    return config.model_copy(
        update={
            "strategies": (strategy,),
            "option": config.option.model_copy(
                update={
                    "no_open_window_start_hour_utc": 0,
                    "no_open_window_end_hour_utc": 0,
                }
            ),
        }
    )


async def command(service, kind, payload):
    row, _ = await service.commands.submit(
        command_id=str(uuid.uuid4()), kind=kind, payload=payload, requested_by="test-operator"
    )
    await service._process_commands()
    async with service.database.sessionmaker() as session:
        return await session.get(type(row), row.id)


async def start_service(database):
    """启动服务并完成基线与接管，让验证编排器拿到执行资格。"""
    config = spread_config()
    expiry = expiry_time()
    market = SpreadMarket(expiry)
    service = TradingService(config, database, market_adapter=market)
    await service.start()
    await command(service, CommandKind.INITIALIZE_BASELINE, {"reason": "stage3"})
    await command(
        service,
        CommandKind.CONFIRM_TAKEOVER,
        {"reason": "stage3", "evidence": "isolated schema, paper only"},
    )
    await service.tick()
    return service, market


async def seed_study(service):
    """建立验证任务、绑定策略版本，并写入一条可用的两腿观察。"""
    store = ValidationStore(service.database.sessionmaker, "paper", service.uid)
    now_ms = int(now_utc().timestamp() * 1000)
    study = await store.start(
        run_id="stage3",
        bases=["ETH"],
        policy="strong_up",
        direction="Call",
        protocol=SpreadProtocol(),
        started_ms=now_ms,
    )
    strategy_id, (_, version) = next(iter(service.strategies.items()))
    await store.bind(study.id, strategy_id=strategy_id, version=version)
    await store.record(
        study.id,
        slot_ms=now_ms,
        observed_ms=now_ms,
        payload={
            "bases": {
                "ETH": {
                    "signal": {"eligible": True},
                    "legs": [asdict(leg) for leg in spread_legs(expiry_time(), now_ms)],
                }
            }
        },
    )
    return store, study


async def intents(database):
    async with database.sessionmaker() as session:
        return list(await session.scalars(select(TradeIntent).order_by(TradeIntent.id)))


async def positions(database):
    async with database.sessionmaker() as session:
        return list(await session.scalars(select(ValidationPosition)))


async def drive_until(service, predicate, limit=16):
    """反复 tick 直到状态满足断言；每轮 tick 走完采样、对账与编排。

    ``predicate`` 是一个**协程函数**，每轮 tick 之后重新调用并等待，
    因此总能看到最新提交的数据库事实。
    """
    for _ in range(limit):
        await service.tick()
        if await predicate():
            return
    raise AssertionError("状态机在限定轮数内没有到达预期状态")


@pytest.mark.asyncio
async def test_two_leg_spread_runs_through_real_service_chain(database):
    """四腿全链路：开多、开空、回补空、平多，各腿都落到持久化事实里。"""
    service, market = await start_service(database)
    try:
        store, study = await seed_study(service)

        # 第一轮：编排器选出候选并提交买腿。
        await service.tick()
        rows = await intents(database)
        assert [row.action for row in rows] == ["OPTION_BUY_OPEN"], rows
        assert rows[0].qty == EXPECTED_QTY
        assert rows[0].validation_position_id is not None
        assert rows[0].coverage_symbol == market.long_symbol

        # 买腿成交：纸面需要下一份盘口，tick 会为挂单重新喂盘口。
        await drive_until(service, has_execution(database, market.long_symbol))
        async with database.sessionmaker() as session:
            long_fill = await session.scalar(
                select(Execution).where(Execution.symbol == market.long_symbol)
            )
            assert long_fill.exec_qty == EXPECTED_QTY
            assert long_fill.exec_price == LONG_ASK

        # 买腿成交后编排器才允许开空；开空占用的是覆盖多头的库存。
        await drive_until(
            service, has_actions(database, "OPTION_BUY_OPEN", "OPTION_SELL_OPEN")
        )
        rows = await intents(database)
        short_intent = rows[-1]
        assert short_intent.symbol == market.short_symbol
        assert short_intent.coverage_symbol == market.long_symbol
        assert short_intent.spend_limit == 0, "开空不占用现金支出额度"
        assert short_intent.reduce_qty is None
        async with database.sessionmaker() as session:
            reservation = await session.scalar(
                select(Reservation).where(Reservation.intent_id == short_intent.id)
            )
            assert reservation.kind == "INVENTORY"
            assert reservation.symbol == market.long_symbol
            decision = await session.scalar(
                select(RiskDecision).where(RiskDecision.intent_id == short_intent.id)
            )
            assert decision.decision == "APPROVED", decision.reason_detail

        # 空头腿成交：纸面按买一成交，产生负债方向的头寸。
        await drive_until(service, has_execution(database, market.short_symbol))
        async with database.sessionmaker() as session:
            short_fill = await session.scalar(
                select(Execution).where(Execution.symbol == market.short_symbol)
            )
            assert short_fill.exec_price == SHORT_BID

        # 持仓阶段：空头腿是负债，账户不得被判成非预期负债而暂停。
        await drive_until(service, has_phase(database, "open"))
        assert not await service.pauses.active_pauses()
        assert service.valuation is not None
        assert service.valuation.complete, service.valuation.incomplete_reason
        assert service.valuation.details["options"]["liabilities"] != "0"

        # 人工停止：编排器先回补空头，再平掉多头。
        async with database.sessionmaker() as session:
            position = await session.scalar(select(ValidationPosition))
            position.state = {**position.state, "stop_requested": True}
            await session.commit()

        await drive_until(
            service,
            has_actions(
                database,
                "OPTION_BUY_OPEN",
                "OPTION_SELL_OPEN",
                "OPTION_BUY_CLOSE",
                "OPTION_SELL_CLOSE",
            ),
        )
        rows = await intents(database)
        close_short, close_long = rows[-2], rows[-1]
        assert close_short.symbol == market.short_symbol
        assert close_short.side == "buy"
        assert close_short.reduce_qty == EXPECTED_QTY
        assert close_long.symbol == market.long_symbol
        assert close_long.side == "sell"
        async with database.sessionmaker() as session:
            short_reservation = await session.scalar(
                select(Reservation).where(Reservation.intent_id == close_short.id)
            )
            assert short_reservation.kind == "CASH"
            long_reservation = await session.scalar(
                select(Reservation).where(Reservation.intent_id == close_long.id)
            )
            assert long_reservation.kind == "INVENTORY"
            assert long_reservation.symbol == market.long_symbol

        # 四腿都成交，库存回到空仓。
        await drive_until(service, has_phase(database, "closed"))
        async with database.sessionmaker() as session:
            inventory = list(
                await session.scalars(select(Inventory).where(Inventory.qty > 0))
            )
            assert not inventory, "全部平仓后不应留下库存"
            orders = list(await session.scalars(select(Order)))
            assert len(orders) == 4
            assert all(order.status == "FILLED" for order in orders), [
                (order.symbol, order.status) for order in orders
            ]
        assert study.id is not None
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_short_leg_cancels_on_pause(database):
    """暂停时撤掉开空腿的挂单：新增敞口的挂单不能留着继续成交。

    这里把空头腿的行情换成**不会成交的报价**（卖一低于空头挂单价），
    让挂单真的挂住；暂停之后 `_cancel_expired` 必须把它撤掉。
    """
    service, market = await start_service(database)
    try:
        store, study = await seed_study(service)
        await service.tick()
        await drive_until(service, has_execution(database, market.long_symbol))
        # 行情先反向（卖一跌到空头挂单价之下），这样开空腿挂得住；
        # 等待意图出现的下一轮 tick 不会把挂单吃掉。
        market.short_book_override = (D("54.4"), D("54.5"))
        await drive_until(
            service, has_actions(database, "OPTION_BUY_OPEN", "OPTION_SELL_OPEN")
        )
        await service.pauses.pause(
            reason_code="ACCOUNT_DEBT", reason_detail="test", locked=True
        )
        await service.tick()
        await service.tick()
        async with database.sessionmaker() as session:
            orders = list(await session.scalars(select(Order)))
            short_orders = [o for o in orders if o.symbol == market.short_symbol]
            assert short_orders, "空头腿应当已经挂出"
            assert all(o.status == "CANCELED" for o in short_orders), [
                (o.symbol, o.status) for o in short_orders
            ]
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_validation_lifecycle_commands(database):
    """开始/停止验证运行命令冻结证据并写入终态，不改变已有研究记录。"""
    service, _ = await start_service(database)
    try:
        start = await command(
            service,
            CommandKind.VALIDATION_START,
            {
                "run_id": "stage4",
                "bases": ["ETH"],
                "policy": "strong_up",
                "direction": "Call",
                "protocol": json_value(asdict(SpreadProtocol())),
                "other_run_ids": [],
            },
        )
        assert start.status == "EXECUTED", start.result
        async with database.sessionmaker() as session:
            study = await session.scalar(
                select(ValidationStudy).where(ValidationStudy.run_id == "stage4")
            )
            assert study is not None
            assert "sources" in study.evidence
            assert "protocol_hash" in study.evidence
            assert "checkpoint_hash" in study.evidence

        stop = await command(
            service, CommandKind.VALIDATION_STOP, {"run_id": "stage4"}
        )
        assert stop.status == "EXECUTED", stop.result
        assert stop.result["status"] == "ended"
        async with database.sessionmaker() as session:
            study = await session.scalar(
                select(ValidationStudy).where(ValidationStudy.run_id == "stage4")
            )
            assert study.status == "ended"
        data = await service.validation.export(
            database, "stage4", environment=service.environment, account_uid=service.uid
        )
        assert data.startswith(b"PK")
    finally:
        await service.close()



@pytest.mark.asyncio
async def test_validation_integrity_failure_fails_closed(database, monkeypatch):
    """源码/账本完整性失败时，验证执行必须锁定暂停而不是继续下单。"""
    service, _ = await start_service(database)
    try:
        await seed_study(service)

        async def broken(*args, **kwargs):
            raise ValueError("product source changed: src/bybitbot/service.py")

        monkeypatch.setattr(service.validation, "check", broken)
        await service.tick()
        pauses = await service.pauses.active_pauses()
        assert any(p.reason_code == "VALIDATION_INTEGRITY" for p in pauses)
        async with database.sessionmaker() as session:
            assert await session.scalar(select(TradeIntent)) is None
    finally:
        await service.close()

def has_execution(database, symbol):
    async def check():
        async with database.sessionmaker() as session:
            return (
                await session.scalar(
                    select(Execution.id).where(Execution.symbol == symbol).limit(1)
                )
            ) is not None

    return check


def has_actions(database, *actions):
    async def check():
        rows = await intents(database)
        return [row.action for row in rows] == list(actions)

    return check


def has_phase(database, phase):
    async def check():
        rows = await positions(database)
        return bool(rows) and rows[0].phase == phase

    return check
