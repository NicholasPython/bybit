from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import replace
from decimal import Decimal as D

import pytest
from sqlalchemy import func, select

from bybitbot.adapters.base import InstrumentSpec, OrderBook, Ticker
from bybitbot.adapters.paper import PaperAdapter
from bybitbot.clock import now_utc
from bybitbot.db.models import (
    CashFlow,
    ControlCommand,
    EquityBaseline,
    Execution,
    Instrument,
    Inventory,
    LedgerEvent,
    Order,
    ScheduleRun,
    TradeIntent,
)
from bybitbot.execution.lease import LeaseState
from bybitbot.ops.control import CommandKind
from bybitbot.service import TradingService
from bybitbot.settings.loader import load_config
from bybitbot.settings.models import Environment
from bybitbot.strategy.base import IntentDraft


class FixedMarket(PaperAdapter):
    def __init__(self):
        super().__init__()
        self.set_instrument(
            InstrumentSpec(
                "spot",
                "BTCUSDT",
                "BTC",
                "USDT",
                "Trading",
                tick_size=D("0.1"),
                qty_step=D("0.001"),
                min_order_qty=D("0.001"),
                min_order_amt=D("1"),
            )
        )

    async def get_ticker(self, category, symbol):
        moment = now_utc()
        return Ticker(
            category,
            symbol,
            bid_price=D("99.9"),
            ask_price=D("100"),
            bid_size=D("100"),
            ask_size=D("100"),
            source_ts=moment,
            received_at=moment,
        )

    async def get_orderbook(self, category, symbol, limit=50):
        moment = now_utc()
        return OrderBook(
            category,
            symbol,
            ((D("99.9"), D("100")),),
            ((D("100"), D("100")),),
            source_ts=moment,
            received_at=moment,
        )


async def command(service, kind, payload):
    row, _ = await service.commands.submit(
        command_id=str(uuid.uuid4()), kind=kind, payload=payload, requested_by="test-operator"
    )
    await service._process_commands()
    return row


@pytest.mark.parametrize("history", [False, True])
async def test_old_fee_checkpoint_only_migrates_without_trading_history(database, history):
    config = load_config("config/config.example.yaml")
    service = TradingService(config, database, market_adapter=FixedMarket())
    adapter = PaperAdapter(
        initial_balances={"USDT": D("731.25")}, account_uid=service.uid,
        account_mode=config.account.account_mode.value,
    )
    saved = adapter.export_state()
    saved["fees"] = {
        "version": "paper-v1-conservative", "spot_taker_rate": "0.001",
        "option_taker_rate": "0.0015",
    }
    if history:
        saved["settlements"] = {"past-settlement": {"payout": "0"}}
    await service.store.put("paper", saved)
    if history:
        with pytest.raises(RuntimeError, match="须人工审计后迁移"):
            await service._adapters()
        assert await service.store.get("paper") == saved
        assert await service.store.get("paper-fee-migration") is None
    else:
        await service._adapters()
        assert (await service.adapter.get_account()).coin("USDT").wallet_balance == D("731.25")
        assert (await service.store.get("paper-fee-migration"))["previous"] == saved
        assert (await service.store.get("paper"))["fees"]["version"] == config.fees.version


async def test_start_baseline_takeover_trade_restart(database):
    config = load_config("config/config.example.yaml")
    service = TradingService(config, database, market_adapter=FixedMarket())
    try:
        await service.start()
        assert service.lease.state is LeaseState.NEEDS_CONFIRMATION
        assert service.machine.state.value == "PAUSED_OPEN"
        assert service.identity_ok and service.reconcile_ok
        assert not service.baseline_ok
        await command(service, CommandKind.INITIALIZE_BASELINE, {"reason": "fixed paper test"})
        await command(
            service,
            CommandKind.CONFIRM_TAKEOVER,
            {"reason": "fixed paper test", "evidence": "no old worker in isolated schema"},
        )
        await service.tick()
        assert service.baseline_ok
        assert service.lease.state is LeaseState.ELIGIBLE
        strategy_id, (_, version) = next(iter(service.strategies.items()))
        draft = IntentDraft(
            action="SPOT_BUY",
            category="spot",
            symbol="BTCUSDT",
            side="Buy",
            qty=D("0.1"),
            qty_unit="BTC",
            currency="USDT",
            spend_limit=D("11"),
            price_limit=D("100"),
            ttl_seconds=60,
        )
        result = await service._submit_draft(strategy_id, version, draft, planned_at=now_utc())
        assert result["status"] == "ACCEPTED", result
        async with database.sessionmaker() as session:
            assert await session.scalar(select(func.count()).select_from(Execution)) == 0
        await service.tick()
        async with database.sessionmaker() as session:
            inventory = await session.scalar(select(Inventory))
            assert inventory.qty == D("0.0999")
            assert await session.scalar(select(func.count()).select_from(Execution)) == 1
            assert (await session.scalar(select(Order))).status == "FILLED"
            assert await session.scalar(select(func.count()).select_from(EquityBaseline)) == 1
        saved = await service.store.get("paper")
    finally:
        await service.close()
    restarted = TradingService(config, database, market_adapter=FixedMarket())
    try:
        await restarted.start()
        assert restarted.lease.state is LeaseState.NEEDS_CONFIRMATION
        assert restarted.baseline_ok
        assert (await restarted.adapter.get_account()).coin("BTC").wallet_balance == D("0.0999")
        assert (await restarted.store.get("paper"))["coins"] == saved["coins"]
        async with database.sessionmaker() as session:
            assert await session.scalar(select(func.count()).select_from(Execution)) == 1
    finally:
        await restarted.close()


class OptionMarket(FixedMarket):
    def __init__(self):
        super().__init__()
        expiry = now_utc() + dt.timedelta(days=10)
        for strike in (80, 100):
            self.set_instrument(
                InstrumentSpec(
                    "option",
                    f"BTC-10NOV26-{strike}-C",
                    "BTC",
                    "USDT",
                    "Trading",
                    tick_size=D("0.001"),
                    qty_step=D("0.1"),
                    min_order_qty=D("0.1"),
                    settle_coin="USDT",
                    options_type="Call",
                    strike_price=D(strike),
                    delivery_time=expiry,
                )
            )

    async def get_ticker(self, category, symbol):
        if category == "spot":
            return await super().get_ticker(category, symbol)
        moment = now_utc()
        return Ticker(
            category,
            symbol,
            bid_price=D("1"),
            ask_price=D("1.001"),
            bid_size=D("100"),
            ask_size=D("100"),
            mark_price=D("1"),
            open_interest=D("50"),
            delta=D("0.5"),
            iv=D("0.6"),
            source_ts=moment,
            received_at=moment,
        )

    async def get_tickers(self, category, *, base_coin=None):
        return [
            await self.get_ticker(category, spec.symbol)
            for spec in await self.get_instruments(category)
            if base_coin is None or spec.base_coin == base_coin
        ]

    async def get_orderbook(self, category, symbol, limit=50):
        if category == "spot":
            return await super().get_orderbook(category, symbol, limit)
        moment = now_utc()
        return OrderBook(
            category,
            symbol,
            ((D("1"), D("100")),),
            ((D("1.001"), D("100")),),
            source_ts=moment,
            received_at=moment,
        )


@pytest.mark.parametrize("category", ["spot", "option"])
async def test_enabled_template_schedule_fills_once_across_restart(database, category):
    config = load_config("config/config.example.yaml")
    selected = config.strategies[0 if category == "spot" else 1]
    selected = selected.model_copy(
        update={
            "enabled": True,
            "symbols": ("BTCUSDT",) if category == "spot" else ("BTC",),
            "schedule_utc": ((now_utc() + dt.timedelta(hours=1)).strftime("%H:%M"),),
            "params": {"max_price_deviation": "0"}
            if category == "spot"
            else {
                "options_type": "Call",
                "min_open_interest": "20",
                "max_premium": "2",
            },
        }
    )
    if category == "option":
        selected = selected.model_copy(
            update={
                "budget_per_run": selected.budget_per_run.model_copy(update={"value": D("2")}),
                "max_spend_per_trade": selected.max_spend_per_trade.model_copy(
                    update={"value": D("2")}
                ),
            }
        )
    config = config.model_copy(
        update={
            "strategies": (selected,),
            "option": config.option.model_copy(
                update={
                    "strike_range_pct": (D("90"), D("110")),
                    "delta_range": (D("0.4"), D("0.6")),
                    "no_open_window_start_hour_utc": 0,
                    "no_open_window_end_hour_utc": 0,
                }
            ),
        }
    )
    service = TradingService(config, database, market_adapter=OptionMarket())
    try:
        await service.start()
        await command(service, CommandKind.INITIALIZE_BASELINE, {"reason": "synthetic scheduler"})
        await command(
            service,
            CommandKind.CONFIRM_TAKEOVER,
            {
                "reason": "synthetic scheduler",
                "evidence": "isolated database and paper only",
            },
        )
        await service.tick()
        strategy_id, (_, version) = next(iter(service.strategies.items()))
        run = await service.scheduler.plan(
            strategy_id=strategy_id,
            strategy_version=version,
            rule_id="synthetic-due",
            planned_at=now_utc(),
            input_snapshot={"symbol": selected.symbols[0]},
        )
        await service._schedule()
        async with database.sessionmaker() as session:
            intent = await session.scalar(select(TradeIntent))
            assert intent is not None
            assert intent.status == "APPROVED"
            if category == "option":
                assert intent.symbol == "BTC-10NOV26-100-C"
            assert (await session.get(ScheduleRun, run.id)).status == "DONE"
            assert await session.scalar(select(func.count()).select_from(Order)) == 1
        await service.tick()
        async with database.sessionmaker() as session:
            assert await session.scalar(select(func.count()).select_from(Execution)) == 1
        await service.close()
        service = TradingService(config, database, market_adapter=OptionMarket())
        await service.start()
        await service._schedule()
        async with database.sessionmaker() as session:
            assert await session.scalar(select(func.count()).select_from(Order)) == 1
    finally:
        await service.close()


async def test_closed_batch_does_not_trigger_exit_of_new_inventory(database):
    """CR-01：全部平仓后的新买入不继承旧批次的持有期计时。

    修复前：第一批两天前买入、昨天平仓；第二批刚买入，持有期 86400 秒，
    退出规则仍由第一批驱动，卖出当前全部库存。修复后新批次重新计时。
    """
    config = load_config("config/config.example.yaml")
    chosen = config.strategies[1].model_copy(update={"symbols": ("BTC",)})
    config = config.model_copy(
        update={
            "strategies": (chosen,),
            "option": config.option.model_copy(
                update={"no_open_window_start_hour_utc": 0, "no_open_window_end_hour_utc": 0}
            ),
        }
    )
    service = TradingService(config, database, market_adapter=OptionMarket())
    try:
        await service.start()
        await command(service, CommandKind.INITIALIZE_BASELINE, {"reason": "episode test"})
        await command(
            service,
            CommandKind.CONFIRM_TAKEOVER,
            {"reason": "episode test", "evidence": "isolated schema"},
        )
        await service.tick()
        strategy_id, (_, version) = next(iter(service.strategies.items()))
        symbol = "BTC-10NOV26-100-C"

        def buy_draft(hold_seconds: int) -> IntentDraft:
            return IntentDraft(
                action="OPTION_BUY_OPEN",
                category="option",
                symbol=symbol,
                side="Buy",
                qty=D("0.1"),
                qty_unit="contract",
                currency="USDT",
                spend_limit=D("1"),
                price_limit=D("1.001"),
                underlying="BTC",
                delivery_time=service.specs[("option", symbol)].delivery_time,
                options_type="Call",
                strike_price=D("100"),
                ttl_seconds=60,
                exit_rules=(
                    {
                        "kind": "hold_period",
                        "hold_seconds": hold_seconds,
                        "exit_validity_seconds": 300,
                        "max_reprice_attempts": 1,
                    },
                ),
            )

        first = await service._submit_draft(
            strategy_id, version, buy_draft(0), planned_at=now_utc(), rule_id="first"
        )
        assert first["status"] == "ACCEPTED", first
        await service.tick()
        async with database.sessionmaker() as session:
            inventory = await session.scalar(select(Inventory))
            first_owner = inventory.episode_intent_id
            first_start = inventory.episode_started_at
            assert first_owner == first["intent_id"]
            assert first_start is not None
        # 持有期为 0：第一轮立即退出并成交，库存归零。
        await service._exit_rules()
        await service.tick()
        await service.tick()
        async with database.sessionmaker() as session:
            inventory = await session.scalar(select(Inventory))
            assert inventory.qty == 0
            sells = list(
                await session.scalars(
                    select(TradeIntent).where(TradeIntent.side == "sell").order_by(TradeIntent.id)
                )
            )
            assert len(sells) == 1
            assert sells[0].signal_id.startswith(f"exit:{first_owner}:")

        # 第二轮买入：持有期未满，不得触发退出。
        second = await service._submit_draft(
            strategy_id, version, buy_draft(86400), planned_at=now_utc(), rule_id="second"
        )
        assert second["status"] == "ACCEPTED", second
        await service.tick()
        async with database.sessionmaker() as session:
            inventory = await session.scalar(select(Inventory))
            assert inventory.qty > 0
            assert inventory.episode_intent_id == second["intent_id"]
            assert inventory.episode_started_at > first_start
        await service._exit_rules()
        await service._exit_rules()
        async with database.sessionmaker() as session:
            sells = list(
                await session.scalars(
                    select(TradeIntent).where(TradeIntent.side == "sell").order_by(TradeIntent.id)
                )
            )
            assert len(sells) == 1, "新库存未满持有期时不应产生退出意图"
    finally:
        await service.close()


async def test_extra_buy_inside_episode_keeps_original_hold_start(database):
    """同一持有周期内追加买入不重置计时起点。"""
    config = load_config("config/config.example.yaml")
    chosen = config.strategies[1].model_copy(update={"symbols": ("BTC",)})
    config = config.model_copy(
        update={
            "strategies": (chosen,),
            "option": config.option.model_copy(
                update={"no_open_window_start_hour_utc": 0, "no_open_window_end_hour_utc": 0}
            ),
        }
    )
    service = TradingService(config, database, market_adapter=OptionMarket())
    try:
        await service.start()
        await command(service, CommandKind.INITIALIZE_BASELINE, {"reason": "episode test"})
        await command(
            service,
            CommandKind.CONFIRM_TAKEOVER,
            {"reason": "episode test", "evidence": "isolated schema"},
        )
        await service.tick()
        strategy_id, (_, version) = next(iter(service.strategies.items()))
        symbol = "BTC-10NOV26-100-C"
        draft = IntentDraft(
            action="OPTION_BUY_OPEN",
            category="option",
            symbol=symbol,
            side="Buy",
            qty=D("0.1"),
            qty_unit="contract",
            currency="USDT",
            spend_limit=D("1"),
            price_limit=D("1.001"),
            underlying="BTC",
            delivery_time=service.specs[("option", symbol)].delivery_time,
            options_type="Call",
            strike_price=D("100"),
            ttl_seconds=60,
            exit_rules=(
                {
                    "kind": "hold_period",
                    "hold_seconds": 86400,
                    "exit_validity_seconds": 300,
                    "max_reprice_attempts": 1,
                },
            ),
        )
        first = await service._submit_draft(
            strategy_id, version, draft, planned_at=now_utc(), rule_id="first"
        )
        assert first["status"] == "ACCEPTED", first
        await service.tick()
        async with database.sessionmaker() as session:
            started = (await session.scalar(select(Inventory))).episode_started_at
        second = await service._submit_draft(
            strategy_id, version, draft, planned_at=now_utc(), rule_id="second"
        )
        assert second["status"] == "ACCEPTED", second
        await service.tick()
        async with database.sessionmaker() as session:
            inventory = await session.scalar(select(Inventory))
            assert inventory.qty > 0
            assert inventory.episode_started_at == started
            assert inventory.episode_intent_id == first["intent_id"]
    finally:
        await service.close()


async def test_option_budget_and_profit_include_full_estimated_fee(database, monkeypatch):
    config = load_config("config/config.example.yaml")
    chosen = config.strategies[1].model_copy(update={"symbols": ("BTC",)})
    config = config.model_copy(update={
        "strategies": (chosen,),
        "option": config.option.model_copy(update={
            "no_open_window_start_hour_utc": 0, "no_open_window_end_hour_utc": 0,
        }),
    })
    service = TradingService(config, database, market_adapter=OptionMarket())
    try:
        await service.start()
        await command(service, CommandKind.INITIALIZE_BASELINE, {"reason": "fee regression"})
        await command(service, CommandKind.CONFIRM_TAKEOVER, {
            "reason": "fee regression", "evidence": "isolated paper schema",
        })
        await service.tick()
        strategy_id, (_, version) = next(iter(service.strategies.items()))
        symbol = "BTC-10NOV26-100-C"
        draft = IntentDraft(
            action="OPTION_BUY_OPEN", category="option", symbol=symbol, side="Buy",
            qty=D("0.1"), qty_unit="contract", currency="USDT", spend_limit=D("0.1004"),
            price_limit=D("1.001"), underlying="BTC", options_type="Call", strike_price=D("100"),
            delivery_time=service.specs[("option", symbol)].delivery_time,
            exit_rules=({"kind": "profit_threshold", "profit_threshold_pct": "10"},),
        )
        rejected = await service._submit_draft(
            strategy_id, version, draft, planned_at=now_utc(), rule_id="underfunded",
        )
        assert rejected["status"] == "REJECTED"
        assert "预算不足以支付价格上限与费用" in rejected["reason"]
        accepted = await service._submit_draft(
            strategy_id, version, replace(draft, spend_limit=D("1")),
            planned_at=now_utc(), rule_id="funded",
        )
        assert accepted["status"] == "ACCEPTED", accepted
        await service.tick()
        submitted = []

        async def collect(*args, **kwargs):
            submitted.append(args[2])
            return {"status": "ACCEPTED"}

        monkeypatch.setattr(service, "_submit_draft", collect)
        for bid in (D("1.2"), D("1.3")):
            quote = replace(
                await service.market_adapter.get_ticker("option", symbol),
                bid_price=bid, ask_price=bid + D("0.001"), mark_price=bid,
            )
            service.quotes.put_ticker(quote)
            await service._exit_rules()
            assert len(submitted) == (0 if bid == D("1.2") else 1)
    finally:
        await service.close()


@pytest.mark.parametrize("category", ["spot", "option"])
async def test_exit_rebuild_preserves_deadline_quantity_and_floor(database, category):
    config = load_config("config/config.example.yaml")
    chosen = config.strategies[0 if category == "spot" else 1].model_copy(
        update={
            "symbols": ("BTCUSDT",) if category == "spot" else ("BTC",),
        }
    )
    config = config.model_copy(
        update={
            "strategies": (chosen,),
            "option": config.option.model_copy(
                update={
                    "no_open_window_start_hour_utc": 0,
                    "no_open_window_end_hour_utc": 0,
                }
            ),
        }
    )
    service = TradingService(config, database, market_adapter=OptionMarket())
    try:
        await service.start()
        await command(service, CommandKind.INITIALIZE_BASELINE, {"reason": "exit test"})
        await command(
            service,
            CommandKind.CONFIRM_TAKEOVER,
            {
                "reason": "exit test",
                "evidence": "isolated schema",
            },
        )
        await service.tick()
        strategy_id, (_, version) = next(iter(service.strategies.items()))
        symbol = "BTCUSDT" if category == "spot" else "BTC-10NOV26-100-C"
        spec = service.specs[(category, symbol)]
        draft = IntentDraft(
            action="SPOT_BUY" if category == "spot" else "OPTION_BUY_OPEN",
            category=category,
            symbol=symbol,
            side="Buy",
            qty=D("0.1"),
            qty_unit="base_coin" if category == "spot" else "contract",
            currency="USDT",
            spend_limit=D("11") if category == "spot" else D("1"),
            price_limit=D("100") if category == "spot" else D("1.001"),
            underlying="BTC",
            delivery_time=spec.delivery_time,
            options_type=spec.options_type,
            strike_price=spec.strike_price,
            exit_rules=(
                {
                    "kind": "hold_period",
                    "hold_seconds": 0,
                    "exit_validity_seconds": 300,
                    "max_reprice_attempts": 1,
                },
            ),
        )
        outcome = await service._submit_draft(strategy_id, version, draft, planned_at=now_utc())
        assert outcome["status"] == "ACCEPTED", outcome
        await service.tick()
        async with database.sessionmaker() as session:
            exit_intent = await session.scalar(
                select(TradeIntent).where(TradeIntent.side == "sell")
            )
            assert exit_intent is not None
            parent = await session.scalar(select(Order).where(Order.intent_id == exit_intent.id))
            assert parent.local_ttl_expires_at <= exit_intent.expires_at
            assert parent.price > exit_intent.price_limit
        assert (await service.executor.cancel(parent)).resolved
        await service._exit_rules()
        async with database.sessionmaker() as session:
            children = list(
                await session.scalars(
                    select(Order)
                    .where(Order.intent_id == exit_intent.id)
                    .order_by(Order.rebuild_index)
                )
            )
            assert len(children) == 2
            child = children[-1]
            assert child.parent_order_id == parent.id
            assert child.rebuild_index == 1
            assert child.qty <= exit_intent.qty
            assert child.price >= exit_intent.price_limit
            assert child.local_ttl_expires_at <= exit_intent.expires_at
        assert (await service.executor.cancel(child)).resolved
        await service._exit_rules()
        await service._exit_rules()
        async with database.sessionmaker() as session:
            assert (
                await session.scalar(
                    select(func.count()).select_from(Order).where(Order.intent_id == exit_intent.id)
                )
                == 2
            )
    finally:
        await service.close()


@pytest.mark.parametrize("settlement_price,payout", [("90", "0"), ("120", "2")])
async def test_paper_settlement_checkpoint_and_ledger_replay(database, settlement_price, payout):
    config = load_config("config/config.example.yaml")
    chosen = config.strategies[1].model_copy(update={"symbols": ("BTC",)})
    config = config.model_copy(
        update={
            "strategies": (chosen,),
            "option": config.option.model_copy(
                update={"no_open_window_start_hour_utc": 0, "no_open_window_end_hour_utc": 0}
            ),
        }
    )
    service = TradingService(config, database, market_adapter=OptionMarket())
    try:
        await service.start()
        await command(service, CommandKind.INITIALIZE_BASELINE, {"reason": "settlement fixture"})
        await command(
            service,
            CommandKind.CONFIRM_TAKEOVER,
            {"reason": "settlement fixture", "evidence": "isolated paper process"},
        )
        await service.tick()
        strategy_id, (_, version) = next(iter(service.strategies.items()))
        symbol = "BTC-10NOV26-100-C"
        draft = IntentDraft(
            action="OPTION_BUY_OPEN",
            category="option",
            symbol=symbol,
            side="Buy",
            qty=D("0.1"),
            qty_unit="contract",
            currency="USDT",
            spend_limit=D("1"),
            price_limit=D("1.001"),
            underlying="BTC",
            options_type="Call",
            strike_price=D("100"),
            delivery_time=service.specs[("option", symbol)].delivery_time,
        )
        assert (await service._submit_draft(strategy_id, version, draft, planned_at=now_utc()))[
            "status"
        ] == "ACCEPTED"
        await service.tick()
        expiry = now_utc() - dt.timedelta(seconds=1)
        spec = replace(service.specs[("option", symbol)], delivery_time=expiry)
        service.adapter.set_instrument(spec)
        async with database.sessionmaker() as session:
            row = await session.scalar(select(Instrument).where(Instrument.symbol == symbol))
            row.delivery_time = expiry
            await session.commit()
        before = (await service.adapter.get_account()).coin("USDT").wallet_balance
        fact = {
            "settlement_id": "synthetic-delivery",
            "symbol": symbol,
            "settlement_price": D(settlement_price),
            "occurred_at": now_utc(),
            "reference": "deterministic-paper-expiry",
        }
        await service.adapter.inject_settlement(**fact)
        await service.close()
        replay_market = OptionMarket()
        replay_market.set_instrument(spec)
        service = TradingService(config, database, market_adapter=replay_market)
        await service.start()
        await service._paper_settlements()
        after = (await service.adapter.get_account()).coin("USDT").wallet_balance
        assert after == before + D(payout)
        async with database.sessionmaker() as session:
            assert (
                await session.scalar(select(Inventory).where(Inventory.symbol == symbol))
            ).qty == 0
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(LedgerEvent)
                    .where(LedgerEvent.event_type == "SETTLEMENT")
                )
                == 1
            )
            assert await session.scalar(select(func.count()).select_from(Execution)) == 1
    finally:
        await service.close()


def live_service_config(config):
    """把示例配置切到非模拟环境，用于测试外部资金流登记。"""
    return config.model_copy(update={"active_environment": Environment.TESTNET})


async def register_flow(service, payload, *, command_id=None):
    row, _ = await service.commands.submit(
        command_id=command_id or str(uuid.uuid4()),
        kind=CommandKind.REGISTER_EXTERNAL_CASH_FLOW,
        payload=payload,
        requested_by="test-operator",
    )
    await service._process_commands()
    async with service.database.sessionmaker() as session:
        return await session.get(ControlCommand, row.id)


async def test_external_cash_flow_is_recorded_with_audit(database):
    config = live_service_config(load_config("config/config.example.yaml"))
    service = TradingService(config, database, market_adapter=FixedMarket())
    service.executor = None
    service.identity_ok = True
    row = await register_flow(service, {
        "asset": "USDT",
        "amount": "5.55",
        "kind": "TRANSFER_IN",
        "reference": "bybit-transfer-20261101",
        "reason": "主账户划入子账户",
    })
    assert row.status == "EXECUTED", row.result
    assert row.result["amount"] == "5.55" and row.result["external"] is True
    async with database.sessionmaker() as session:
        flow = await session.scalar(select(CashFlow))
        assert flow is not None
        assert flow.asset == "USDT" and flow.amount == D("5.55")
        assert flow.kind == "TRANSFER_IN" and flow.external is True
        event = await session.scalar(
            select(LedgerEvent).where(
                LedgerEvent.event_type == "REGISTER_EXTERNAL_CASH_FLOW"
            )
        )
        assert event is not None
        assert event.payload["operator"] == "test-operator"
        assert event.payload["reference"] == "bybit-transfer-20261101"


async def test_external_cash_flow_direction_follows_kind(database):
    config = live_service_config(load_config("config/config.example.yaml"))
    service = TradingService(config, database, market_adapter=FixedMarket())
    service.executor = None
    service.identity_ok = True
    row = await register_flow(service, {
        "asset": "USDT",
        "amount": "2.5",
        "kind": "WITHDRAWAL",
        "reference": "bybit-withdrawal-1",
        "reason": "提现",
    })
    assert row.status == "EXECUTED", row.result
    assert row.result["amount"] == "-2.5"
    async with database.sessionmaker() as session:
        flow = await session.scalar(select(CashFlow))
        assert flow.amount == D("-2.5")


async def test_external_cash_flow_cannot_be_registered_twice(database):
    config = live_service_config(load_config("config/config.example.yaml"))
    service = TradingService(config, database, market_adapter=FixedMarket())
    service.executor = None
    service.identity_ok = True
    payload = {
        "asset": "USDT",
        "amount": "5.55",
        "kind": "TRANSFER_IN",
        "reference": "bybit-transfer-20261101",
        "reason": "主账户划入子账户",
    }
    first = await register_flow(service, payload)
    assert first.status == "EXECUTED", first.result
    second = await register_flow(service, payload)
    assert second.status == "REJECTED"
    async with database.sessionmaker() as session:
        assert await session.scalar(select(func.count()).select_from(CashFlow)) == 1


@pytest.mark.parametrize(
    ("payload", "environment", "identity", "expected"),
    [
        (
            {"asset": "BTC", "amount": "1", "kind": "DEPOSIT", "reference": "r1", "reason": "x"},
            "testnet", True, "只能登记估值币种",
        ),
        (
            {"asset": "USDT", "amount": "0", "kind": "DEPOSIT", "reference": "r2", "reason": "x"},
            "testnet", True, "金额必须为正数",
        ),
        (
            {"asset": "USDT", "amount": "1", "kind": "UNKNOWN", "reference": "r3", "reason": "x"},
            "testnet", True, "未知的外部资金流类型",
        ),
        (
            {"asset": "USDT", "amount": "1", "kind": "DEPOSIT", "reference": "", "reason": "x"},
            "testnet", True, "交易所侧的资金流水标识",
        ),
        (
            {"asset": "USDT", "amount": "1", "kind": "DEPOSIT", "reference": "r5", "reason": "x"},
            "testnet", False, "账户身份未通过验证",
        ),
        (
            {"asset": "USDT", "amount": "1", "kind": "DEPOSIT", "reference": "r6", "reason": "x"},
            "paper", True, "模拟环境不登记外部资金流",
        ),
    ],
)
async def test_external_cash_flow_rejects_unusable_input(
    database, payload, environment, identity, expected
):
    config = live_service_config(load_config("config/config.example.yaml"))
    if environment == "paper":
        config = config.model_copy(update={"active_environment": Environment.PAPER})
    service = TradingService(config, database, market_adapter=FixedMarket())
    service.executor = None
    service.identity_ok = identity
    row = await register_flow(service, payload)
    assert row.status == "REJECTED"
    assert expected in row.result["error"]
    async with database.sessionmaker() as session:
        assert await session.scalar(select(func.count()).select_from(CashFlow)) == 0
