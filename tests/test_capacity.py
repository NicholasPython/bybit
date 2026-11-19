"""容量、磁盘与保留清理（AC-22 / NFR-01 / NFR-02 / NFR-05）。

需求原文：
    **NFR-01** 首期容量：1 个账户、最多 5 个策略实例、10 个现货交易对、200 个候选期权合约；
    策略合计提交意图不超过 1 次/秒，低于交易所实际限额运行。超出范围必须另做容量及订阅验证。
    候选目录、报价刷新与深度订阅分层，不要求全市场逐笔保存。

    **NFR-02** 在指定容量、4 vCPU/8GB 参考环境、数据库正常且不含外部网络/API等待时，
    意图校验与持久化预留处理 P95 ≤ 500ms。

    **NFR-05** 交易、配置、风控与审计记录建议保留至少 1 年；原始行情短期保留 7 天，
    可按空间调整；按策略决策保存的必要行情快照随审计记录保留。磁盘容量设告警及暂停阈值。
    达到容量边界时停止新增风险，不静默丢弃账务记录。

    **AC-22** 在指定容量下记录延迟分布、CPU/内存、订阅量和磁盘增长；满足性能基线。
    模拟磁盘不足，触发暂停且账务记录无静默丢失。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import platform
import resource
import shutil
import uuid
from decimal import Decimal as D
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from sqlalchemy import func, select, text

from bybitbot.adapters.base import InstrumentSpec, OrderBook, Ticker
from bybitbot.adapters.paper import PaperAdapter
from bybitbot.clock import now_utc
from bybitbot.db.models import (
    Execution,
    Inventory,
    LedgerEvent,
    Order,
    QuoteSnapshot,
    RiskPause,
    ScheduleRun,
    TradeIntent,
)
from bybitbot.ops.control import CommandKind
from bybitbot.ops.pauses import PauseReason
from bybitbot.ops.ratelimit import SlidingWindowRateLimiter
from bybitbot.ops.retention import RetentionCleaner
from bybitbot.ops.state import RunState
from bybitbot.service import TradingService
from bybitbot.settings.loader import ConfigError, load_config
from bybitbot.settings.models import RuntimeConfig, StrategyConfig
from bybitbot.strategy.base import IntentDraft

SPOT_PAIRS = (
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "XRPUSDT",
    "ADAUSDT",
    "DOGEUSDT",
    "LTCUSDT",
    "BCHUSDT",
)
OPTION_BASES = ("ETH",)
OPTION_CONTRACTS = 200


# --------------------------------------------------------------------------
# 配置与合成行情
# --------------------------------------------------------------------------


def future_slot(hours: int = 2) -> str:
    """一个不会立刻到期的 UTC 计划时刻（小时级偏移，跨日也只是被跳过）。"""
    return (now_utc() + dt.timedelta(hours=hours)).strftime("%H:%M")


class FakeMonotonic:
    """可控的单调钟：限速窗口推进不依赖真实时间。"""

    def __init__(self) -> None:
        self.value = 1000.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


async def _intent_count(database) -> int:
    async with database.sessionmaker() as session:
        return await session.scalar(select(func.count()).select_from(TradeIntent))


def spot_strategy(name: str, symbols: tuple[str, ...]) -> StrategyConfig:
    return StrategyConfig(
        name=name,
        kind="spot_fixed_budget_dca",
        enabled=True,
        symbols=symbols,
        schedule_utc=(future_slot(),),
        signal_ttl_seconds=300,
        cooldown_seconds=3600,
        budget_per_run={"value": "40", "currency": "USDT"},
        max_spend_per_trade={"value": "20", "currency": "USDT"},
        exit_rules=(
            {
                "kind": "hold_period",
                "hold_seconds": 86400,
                "max_reprice_attempts": 1,
                "exit_validity_seconds": 300,
            },
        ),
    )


def option_strategy(name: str, base: str) -> StrategyConfig:
    return StrategyConfig(
        name=name,
        kind="option_single_leg_buy",
        enabled=True,
        symbols=(base,),
        schedule_utc=(future_slot(),),
        signal_ttl_seconds=120,
        cooldown_seconds=604800,
        budget_per_run={"value": "20", "currency": "USDT"},
        max_spend_per_trade={"value": "20", "currency": "USDT"},
        params={
            "options_type": "Call",
            "min_days_to_expiry": "7",
            "max_days_to_expiry": "45",
            "delta_low": "0.30",
            "delta_high": "0.60",
            "strike_low_pct": "90",
            "strike_high_pct": "110",
            "min_premium": "50",
            "max_premium": "200",
            "max_fee_ratio": "0.01",
        },
        exit_rules=(
            {
                "kind": "premium_stop_loss",
                "stop_loss_pct": "25",
                "max_reprice_attempts": 1,
                "exit_validity_seconds": 300,
            },
        ),
    )


def capacity_config():
    """首期容量配置：5 个策略实例、8 个现货交易对、200 个候选期权合约。"""
    config = load_config("config/config.example.yaml")
    strategies = (
        spot_strategy("cap-spot-a", ("BTCUSDT", "ETHUSDT")),
        spot_strategy("cap-spot-b", ("SOLUSDT", "XRPUSDT")),
        spot_strategy("cap-spot-c", ("ADAUSDT", "DOGEUSDT")),
        spot_strategy("cap-spot-d", ("LTCUSDT", "BCHUSDT")),
        option_strategy("cap-option-e", "ETH"),
    )
    return config.model_copy(
        update={
            "strategies": strategies,
            "runtime": config.runtime.model_copy(update={"max_option_candidates": 200}),
        }
    )


async def command(service, kind, payload):
    row, _ = await service.commands.submit(
        command_id=str(uuid.uuid4()), kind=kind, payload=payload, requested_by="test-operator"
    )
    await service._process_commands()
    async with service.database.sessionmaker() as session:
        return await session.get(type(row), row.id)


async def start_service(database, *, config=None, market=None):
    """启动并接管的服务：已建立风险基线，可以正常跑。"""
    service = TradingService(
        config or capacity_config(),
        database,
        market_adapter=market if market is not None else CapacityMarket(),
    )
    await service.start()
    await command(service, CommandKind.INITIALIZE_BASELINE, {"reason": "容量证据测试"})
    await command(
        service,
        CommandKind.CONFIRM_TAKEOVER,
        {"reason": "容量证据测试", "evidence": "隔离 schema 内的唯一 worker"},
    )
    await service.tick()
    return service


def write_config(tmp_path: Path, strategies: list[dict]) -> Path:
    """写一份带指定策略段的配置文件。"""
    raw = yaml.safe_load(Path("config/config.example.yaml").read_text(encoding="utf-8"))
    raw["strategies"] = strategies
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


class CapacityMarket(PaperAdapter):
    """容量测试用的合成行情源。

    记录深度订阅次数与批量报价次数 —— 用来说明"候选目录、报价刷新与深度订阅分层"。
    """

    def __init__(
        self,
        spot_symbols: tuple[str, ...] = SPOT_PAIRS,
        option_bases: tuple[str, ...] = OPTION_BASES,
        option_contracts: int = OPTION_CONTRACTS,
    ):
        super().__init__()
        self.depth_calls = 0
        self.ticker_calls = 0
        self.bulk_ticker_calls = 0
        self._option_specs: list[InstrumentSpec] = []
        for symbol in spot_symbols:
            self.set_instrument(
                InstrumentSpec(
                    "spot",
                    symbol,
                    symbol.removesuffix("USDT"),
                    "USDT",
                    "Trading",
                    tick_size=D("0.1"),
                    qty_step=D("0.001"),
                    min_order_qty=D("0.001"),
                    min_order_amt=D("1"),
                )
            )
            self.set_ticker(self._spot_ticker(symbol))
        for base in option_bases:
            for spec in self._option_instruments(base, option_contracts):
                self.set_instrument(spec)
                self.set_ticker(self._option_ticker(spec))
                self._option_specs.append(spec)

    def _spot_ticker(self, symbol: str) -> Ticker:
        moment = now_utc()
        return Ticker(
            "spot",
            symbol,
            bid_price=D("99.9"),
            ask_price=D("100"),
            bid_size=D("1000"),
            ask_size=D("1000"),
            source_ts=moment,
            received_at=moment,
        )

    def _option_instruments(self, base: str, count: int) -> list[InstrumentSpec]:
        delivery = now_utc() + dt.timedelta(days=20)
        specs = []
        for index in range(count):
            specs.append(
                InstrumentSpec(
                    "option",
                    f"{base}-20DEC26-{3000 + index}-C",
                    base,
                    "USDT",
                    "Trading",
                    tick_size=D("0.1"),
                    qty_step=D("0.001"),
                    min_order_qty=D("0.001"),
                    min_order_amt=D("1"),
                    strike_price=D("3000"),
                    delivery_time=delivery,
                    options_type="Call",
                    settle_coin="USDT",
                )
            )
        return specs

    def _option_ticker(self, spec: InstrumentSpec) -> Ticker:
        moment = now_utc()
        return Ticker(
            "option",
            spec.symbol,
            bid_price=D("100"),
            ask_price=D("100.1"),
            bid_size=D("10"),
            ask_size=D("10"),
            mark_price=D("100.05"),
            index_price=D("3000"),
            delta=D("0.45"),
            iv=D("0.6"),
            open_interest=D("1000"),
            source_ts=moment,
            received_at=moment,
        )

    async def get_ticker(self, category, symbol):
        self.ticker_calls += 1
        ticker = self._tickers.get(symbol)
        if ticker is None:
            raise ValueError(f"未注册的合约 {symbol}")
        return ticker

    async def get_orderbook(self, category, symbol, limit=50):
        self.depth_calls += 1
        moment = now_utc()
        return OrderBook(
            category,
            symbol,
            ((D("99.9"), D("100")),),
            ((D("100"), D("100")),),
            source_ts=moment,
            received_at=moment,
        )

    async def get_tickers(self, category, *, base_coin=None):
        self.bulk_ticker_calls += 1
        return [t for t in self._tickers.values() if t.category == category]


# --------------------------------------------------------------------------
# A. 首期容量边界
# --------------------------------------------------------------------------


def test_first_phase_capacity_configuration_is_accepted():
    config = capacity_config()
    assert len(config.strategies) == 5
    assert config.runtime.max_option_candidates == 200


def test_more_than_five_strategies_is_rejected(tmp_path):
    strategies = [
        yaml.safe_load(spot_strategy(f"cap-{index}", ("BTCUSDT",)).model_dump_json())
        for index in range(6)
    ]
    path = write_config(tmp_path, strategies)
    with pytest.raises(ConfigError, match="NFR-01"):
        load_config(path)


def test_more_than_ten_spot_pairs_is_rejected(tmp_path):
    pairs = [f"COIN{index}USDT" for index in range(11)]
    strategies = [
        yaml.safe_load(spot_strategy("cap-wide", tuple(pairs)).model_dump_json()),
    ]
    path = write_config(tmp_path, strategies)
    with pytest.raises(ConfigError, match="NFR-01"):
        load_config(path)


def test_option_candidate_limit_cannot_exceed_two_hundred():
    with pytest.raises(ValidationError, match="200"):
        RuntimeConfig(max_option_candidates=201)


# --------------------------------------------------------------------------
# B. 保留清理
# --------------------------------------------------------------------------


async def count_rows(database, model) -> int:
    async with database.sessionmaker() as session:
        return int(await session.scalar(select(func.count()).select_from(model)) or 0)


async def table_bytes(database, tables: tuple[str, ...]) -> dict[str, int]:
    """各表占用的磁盘字节（含索引），用于记录磁盘增长。"""
    async with database.sessionmaker() as session:
        sizes = {}
        for table in tables:
            size = await session.scalar(
                text("SELECT pg_total_relation_size(:name)"), {"name": table}
            )
            sizes[table] = int(size or 0)
    return sizes


async def test_retention_removes_expired_raw_quotes_but_keeps_decision_evidence(database):
    cleaner = RetentionCleaner(
        database.sessionmaker, "paper", raw_market_data_days=7, retention_days=365
    )
    old = now_utc() - dt.timedelta(days=10)
    async with database.sessionmaker() as session:
        expired = QuoteSnapshot(
            environment="paper",
            category="spot",
            symbol="BTCUSDT",
            received_at=old,
            raw={},
        )
        kept = QuoteSnapshot(
            environment="paper",
            category="spot",
            symbol="ETHUSDT",
            received_at=old,
            raw={},
        )
        fresh = QuoteSnapshot(
            environment="paper",
            category="spot",
            symbol="SOLUSDT",
            received_at=now_utc(),
            raw={},
        )
        session.add_all([expired, kept, fresh])
        await session.commit()
        await session.refresh(expired)
        await session.refresh(kept)
        intent = TradeIntent(
            environment="paper",
            account_uid="paper-account",
            signal_id="decision-1",
            action="SPOT_BUY",
            category="spot",
            symbol="ETHUSDT",
            side="buy",
            qty=D("0.01"),
            qty_unit="BTC",
            spend_limit=D("1"),
            currency="USDT",
            expires_at=now_utc() + dt.timedelta(seconds=60),
            status="APPROVED",
            quote_snapshot_id=kept.id,
            created_at=now_utc(),
        )
        session.add(intent)
        await session.commit()

    report = await cleaner.clean()
    assert report.raw_market_data_deleted == 1
    assert report.decision_snapshots_kept == 1

    async with database.sessionmaker() as session:
        remaining = list(
            await session.scalars(
                select(QuoteSnapshot).where(QuoteSnapshot.environment == "paper")
            )
        )
        stored = await session.scalar(
            select(TradeIntent).where(TradeIntent.signal_id == "decision-1")
        )
    assert {row.symbol for row in remaining} == {"ETHUSDT", "SOLUSDT"}
    # 决策引用的快照还在，引用没有被静默抹掉。
    assert stored.quote_snapshot_id == kept.id


async def test_retention_never_deletes_accounting_records(database):
    cleaner = RetentionCleaner(
        database.sessionmaker, "paper", raw_market_data_days=7, retention_days=365
    )
    async with database.sessionmaker() as session:
        session.add(
            LedgerEvent(
                environment="paper",
                category="trade",
                event_type="INTENT",
                dedupe_key="intent:1",
                payload={},
                occurred_at=now_utc() - dt.timedelta(days=400),
                recorded_at=now_utc(),
            )
        )
        session.add(
            QuoteSnapshot(
                environment="paper",
                category="spot",
                symbol="BTCUSDT",
                received_at=now_utc() - dt.timedelta(days=400),
                raw={},
            )
        )
        await session.commit()

    before = {
        model: await count_rows(database, model)
        for model in (LedgerEvent, Execution, Order, Inventory, TradeIntent)
    }
    await cleaner.clean()
    after = {
        model: await count_rows(database, model)
        for model in (LedgerEvent, Execution, Order, Inventory, TradeIntent)
    }
    assert before == after
    assert before[LedgerEvent] == 1


async def test_retention_is_idempotent(database):
    cleaner = RetentionCleaner(database.sessionmaker, "paper", raw_market_data_days=7)
    async with database.sessionmaker() as session:
        session.add(
            QuoteSnapshot(
                environment="paper",
                category="spot",
                symbol="BTCUSDT",
                received_at=now_utc() - dt.timedelta(days=30),
                raw={},
            )
        )
        await session.commit()
    first = await cleaner.clean()
    second = await cleaner.clean()
    assert first.raw_market_data_deleted == 1
    assert second.raw_market_data_deleted == 0


async def test_service_runs_retention_once_per_day_and_records_the_report(database):
    service = await start_service(database)
    try:
        state = await service.store.get("retention")
        assert state is not None
        assert "raw_market_data_deleted" in state
        ran_at = state["ran_at"]
        await service.tick()
        assert (await service.store.get("retention"))["ran_at"] == ran_at
    finally:
        await service.close()


# --------------------------------------------------------------------------
# C. 磁盘不足
# --------------------------------------------------------------------------


def fake_disk_usage(free_gb: float):
    class _Usage:
        total = int(free_gb * 1024**3 * 2)
        used = int(free_gb * 1024**3)
        free = int(free_gb * 1024**3)

    return _Usage()


async def test_disk_pressure_pauses_new_exposure_without_losing_records(
    database, monkeypatch
):
    service = await start_service(database)
    try:
        strategy_id, (_, version) = next(iter(service.strategies.items()))
        await service._submit_draft(
            strategy_id,
            version,
            IntentDraft(
                action="SPOT_BUY",
                category="spot",
                symbol="BTCUSDT",
                side="Buy",
                qty=D("0.05"),
                qty_unit="BTC",
                currency="USDT",
                spend_limit=D("6"),
                price_limit=D("100"),
                ttl_seconds=60,
            ),
            planned_at=now_utc(),
            rule_id="disk-baseline",
        )
        monkeypatch.setattr(shutil, "disk_usage", lambda path: fake_disk_usage(1))
        await service.tick()

        assert service.machine.state is RunState.PAUSED_OPEN
        assert not service.machine.allows_new_exposure
        async with database.sessionmaker() as session:
            pause = await session.scalar(
                select(RiskPause).where(
                    RiskPause.environment == "paper",
                    RiskPause.reason_code == PauseReason.DISK_PRESSURE,
                    RiskPause.active.is_(True),
                )
            )
        assert pause is not None

        # 账务记录一条不少 —— 磁盘紧张时停止新增风险，而不是丢弃记录。
        before = {
            model: await count_rows(database, model)
            for model in (LedgerEvent, Execution, Order, Inventory, TradeIntent)
        }
        assert before[TradeIntent] == 1
        await service.tick()
        after = {
            model: await count_rows(database, model)
            for model in (LedgerEvent, Execution, Order, Inventory, TradeIntent)
        }
        assert after == before

        # 空间恢复后技术暂停自动解除。
        monkeypatch.setattr(shutil, "disk_usage", lambda path: fake_disk_usage(80))
        await service.tick()
        assert service.machine.state is RunState.RUNNING
    finally:
        await service.close()


async def test_disk_warning_below_free_threshold_does_not_pause(database, monkeypatch):
    service = await start_service(database)
    try:
        monkeypatch.setattr(shutil, "disk_usage", lambda path: fake_disk_usage(10))
        await service.tick()
        keys = {view.alert_key for view in await service.alerts.active()}
        assert "DISK_WARNING" in keys
        assert service.machine.state is RunState.RUNNING
    finally:
        await service.close()


# --------------------------------------------------------------------------
# D. 容量负载、延迟分布与订阅量
# --------------------------------------------------------------------------


def max_rss_mb() -> float:
    """峰值常驻内存（MiB）；Linux 的 ru_maxrss 是 KiB，macOS 是字节。"""
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if platform.system() == "Darwin":
        return raw / 1024**2
    return raw / 1024


async def test_capacity_load_records_latency_within_the_baseline(database):
    """NFR-02：指定容量下意图校验与持久化预留 P95 ≤ 500ms。"""
    config = capacity_config()
    market = CapacityMarket()
    service = await start_service(database, config=config, market=market)
    try:
        spot_ids = [
            sid
            for sid, (cfg, _) in service.strategies.items()
            if cfg.kind == "spot_fixed_budget_dca"
        ]
        cpu_before = resource.getrusage(resource.RUSAGE_SELF)
        tables = ("quote_snapshot", "trade_intent", "order", "ledger_event")
        sizes_before = await table_bytes(database, tables)
        rounds = 15
        for index in range(rounds):
            strategy_id = spot_ids[index % len(spot_ids)]
            _, version = service.strategies[strategy_id]
            symbol = service.strategies[strategy_id][0].symbols[index % 2]
            await service._submit_draft(
                strategy_id,
                version,
                IntentDraft(
                    action="SPOT_BUY",
                    category="spot",
                    symbol=symbol,
                    side="Buy",
                    qty=D("0.05"),
                    qty_unit="BTC",
                    currency="USDT",
                    spend_limit=D("6"),
                    price_limit=D("100"),
                    ttl_seconds=60,
                ),
                planned_at=now_utc(),
                rule_id=f"load-{index}",
            )
        cpu_after = resource.getrusage(resource.RUSAGE_SELF)
        sizes_after = await table_bytes(database, tables)
        summary = service.latency.summary("intent_validation")
        assert summary is not None
        assert summary.count == rounds

        cpu_seconds = (cpu_after.ru_utime - cpu_before.ru_utime) + (
            cpu_after.ru_stime - cpu_before.ru_stime
        )
        evidence = {
            "generated_at": now_utc().isoformat(),
            "environment": "paper",
            "capacity": {
                "strategies": len(config.strategies),
                "spot_pairs": len(SPOT_PAIRS),
                "option_candidates": config.runtime.max_option_candidates,
                "catalog_option_contracts": len(
                    [spec for spec in service.specs.values() if spec.category == "option"]
                ),
            },
            "latency_intent_validation_ms": summary.as_dict(),
            "baseline_p95_ms": 500,
            "cpu_seconds": round(cpu_seconds, 3),
            "cpu_seconds_per_intent": round(cpu_seconds / rounds, 4),
            "max_rss_mb": round(max_rss_mb(), 1),
            "bulk_option_quote_calls": market.bulk_ticker_calls,
            "depth_subscription_calls": market.depth_calls,
            "disk_growth_bytes": {
                table: sizes_after[table] - sizes_before[table] for table in tables
            },
        }
        path = os.environ.get("BYBITBOT_EVIDENCE_PATH")
        if path:
            await asyncio.to_thread(
                Path(path).write_text,
                json.dumps(evidence, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

        assert summary.p95_ms <= 500, summary.as_dict()
        assert summary.max_ms <= 500, summary.as_dict()
        # 候选目录、报价刷新与深度订阅分层：深度订阅只覆盖真正需要的品种，
        # 不是全部候选合约。
        assert len([spec for spec in service.specs.values() if spec.category == "option"]) == (
            OPTION_CONTRACTS
        )
        assert market.depth_calls <= len(SPOT_PAIRS) * 4
        assert market.bulk_ticker_calls >= 1
    finally:
        await service.close()


async def test_scheduled_intents_respect_the_per_second_limit(database):
    """NFR-01：策略合计每秒最多提交一个意图，超出的计划被延后而不是丢弃。"""
    service = await start_service(database)
    try:
        # 用可控单调钟替换限速器：窗口推进不依赖真实时间，测试是确定的。
        clock = FakeMonotonic()
        service.intent_limiter = SlidingWindowRateLimiter(1, monotonic=clock)

        planned = []
        for strategy_id, (_, version) in list(service.strategies.items())[:2]:
            cfg, _ = service.strategies[strategy_id]
            run = await service.scheduler.plan(
                strategy_id=strategy_id,
                strategy_version=version,
                rule_id="rate-limit",
                planned_at=now_utc(),
                input_snapshot={"symbol": cfg.symbols[0]},
            )
            assert run is not None
            planned.append(run.id)

        await service._schedule()

        async with database.sessionmaker() as session:
            rows = {
                row.id: row
                for row in await session.scalars(
                    select(ScheduleRun).where(ScheduleRun.id.in_(planned))
                )
            }
        statuses = sorted(rows[run_id].status for run_id in planned)
        # 同一秒的两个计划：第一个提交，第二个被延后（仍是 PLANNED，不是不存在）。
        assert statuses == ["DONE", "PLANNED"]
        deferred = next(row for row in rows.values() if row.status == "PLANNED")
        assert deferred.result["deferred"] is True
        assert deferred.result["reason"] == "超过每秒意图上限"
        assert deferred.result["limit_per_second"] == 1
        keys = {view.alert_key for view in await service.alerts.active()}
        assert "INTENT_RATE_LIMIT" in keys
        assert await _intent_count(database) == 1

        # 窗口过去后重新调度：延后的计划被补上，且不会重复提交前一个。
        clock.advance(1.5)
        await service._schedule()

        async with database.sessionmaker() as session:
            rows = {
                row.id: row
                for row in await session.scalars(
                    select(ScheduleRun).where(ScheduleRun.id.in_(planned))
                )
            }
            intents = list(
                await session.scalars(
                    select(TradeIntent).order_by(TradeIntent.id)
                )
            )
        # 延后的计划不再停留在 PLANNED：它被真正执行了（提交意图后由风控判定）。
        deferred_run = rows[deferred.id]
        assert deferred_run.status in {"DONE", "REJECTED"}
        assert len(intents) == 2
        # 两个计划各留下一个意图：前一个没有被重放。
        assert len({intent.strategy_id for intent in intents}) == 2
    finally:
        await service.close()
