"""常驻服务（STR-02 / RSK-01..05 / EXE-05 / OPS-01..05）。

Web 与 worker 分进程；唯一 worker 持有会话锁。Paper 撮合结果先保存检查点，
再以成交编号幂等入账，发送中崩溃不会把模拟现金重置成初始资金。
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import hashlib
import json
import logging
import shutil
import time
from dataclasses import asdict, replace
from decimal import Decimal, InvalidOperation
from pathlib import Path

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert

from bybitbot.adapters.bybit_rest import BybitRestAdapter
from bybitbot.adapters.paper import FeeModelMismatch, PaperAdapter
from bybitbot.clock import local_day, local_day_start_utc, now_utc
from bybitbot.db.models import (
    AccountSnapshot,
    CashFlow,
    ControlCommand,
    EquityBaseline,
    EquitySnapshot,
    Execution,
    Inventory,
    LedgerEvent,
    Order,
    Reservation,
    RiskDecision,
    RiskPause,
    RuntimeState,
    ScheduleRun,
    Strategy,
    StrategyConfigVersion,
    TradeIntent,
)
from bybitbot.db.session import Database
from bybitbot.execution.executor import Executor, OrderPlan
from bybitbot.execution.lease import ExecutorLease, LeaseState
from bybitbot.execution.states import TERMINAL_STATES
from bybitbot.ledger import LedgerWriter
from bybitbot.market import InstrumentCatalog, QuoteCache
from bybitbot.money import Rounding, round_to_step
from bybitbot.ops.alerts import AlertManager
from bybitbot.ops.control import CommandKind, CommandStatus, ControlCommandManager
from bybitbot.ops.notifications import deliver_alerts
from bybitbot.ops.pauses import PauseManager, PauseReason
from bybitbot.ops.state import RunState, RunStateMachine
from bybitbot.reconciliation import Reconciler
from bybitbot.risk.engine import OpenOrderExposure, RiskContext, RiskEngine
from bybitbot.risk.reservation import ReservationError, ReservationManager
from bybitbot.scheduler import Scheduler
from bybitbot.settings.loader import AppConfig, load_config
from bybitbot.settings.secrets import load_credentials
from bybitbot.strategy.base import IntentDraft, SignalContext, build_intent
from bybitbot.strategy.templates import (
    OptionBuyParams,
    ScheduledOptionBuyStrategy,
    ScheduledSpotBuyStrategy,
    SpotBuyParams,
)
from bybitbot.valuation import ValuationInput, ValuationPolicy, value_account

logger = logging.getLogger(__name__)
ZERO = Decimal("0")
OPEN_ACTIONS = ("BUY_OPEN", "SPOT_BUY", "OPTION_BUY_OPEN")


def json_value(value):
    return json.loads(
        json.dumps(
            value, default=lambda obj: obj.isoformat() if isinstance(obj, dt.datetime) else str(obj)
        )
    )


def config_fingerprint(config: AppConfig) -> str:
    safe = config.model_dump(mode="json", exclude={"environments"})
    return hashlib.sha256(json.dumps(safe, sort_keys=True).encode()).hexdigest()[:20]


class StateStore:
    def __init__(self, database: Database, environment: str):
        self.database, self.environment = database, environment

    async def get(self, key):
        async with self.database.sessionmaker() as session:
            return await session.scalar(
                select(RuntimeState.value).where(
                    RuntimeState.environment == self.environment, RuntimeState.key == key
                )
            )

    async def put(self, key, value):
        async with self.database.sessionmaker() as session:
            statement = insert(RuntimeState).values(
                environment=self.environment, key=key, value=json_value(value), updated_at=now_utc()
            )
            await session.execute(
                statement.on_conflict_do_update(
                    constraint="uq_runtime_state",
                    set_={
                        "value": statement.excluded.value,
                        "updated_at": statement.excluded.updated_at,
                    },
                )
            )
            await session.commit()


class DurablePaperAdapter(PaperAdapter):
    def __init__(self, store: StateStore, **kwargs):
        super().__init__(**kwargs)
        self.store = store

    async def checkpoint(self):
        await self.store.put("paper", self.export_state())

    async def create_order(self, request):
        try:
            return await super().create_order(request)
        finally:
            await self.checkpoint()

    async def inject_settlement(self, **fact):
        event = self.settle_option(**fact)
        await self.checkpoint()
        return event

    async def cancel_order(self, *args, **kwargs):
        try:
            return await super().cancel_order(*args, **kwargs)
        finally:
            await self.checkpoint()


class DecisionView:
    """策略只看到已验证行情和规格，无法调用交易适配器。"""

    def __init__(self, specs, quotes, moment):
        self.specs, self.quotes, self.now = specs, quotes, moment

    def instrument(self, category, symbol):
        return self.specs.get((category, symbol))

    def instruments(self, category):
        return [item for (kind, _), item in self.specs.items() if kind == category]

    def quote(self, category, symbol):
        return self.quotes.get_ticker(category, symbol)

    def config(self):
        return {}


class TradingService:
    #: 外部资金流类型与方向（LED-03 / LED-04）。金额一律填正数，
    #: 方向由类型决定，避免把提现登记成充值。
    _EXTERNAL_FLOW_SIGNS = {
        "DEPOSIT": 1,
        "TRANSFER_IN": 1,
        "WITHDRAWAL": -1,
        "TRANSFER_OUT": -1,
    }

    def __init__(
        self,
        config: AppConfig,
        database: Database,
        *,
        config_path=None,
        adapter=None,
        market_adapter=None,
    ):
        self.config, self.database = config, database
        self.config_path = Path(config_path) if config_path else None
        self.environment = config.active_environment.value
        self.uid = config.account.expected_uid or "paper-account"
        self.store = StateStore(database, self.environment)
        self.adapter, self.market_adapter = adapter, market_adapter
        self._owned_adapters = []
        self.catalog = InstrumentCatalog(database.sessionmaker, self.environment)
        self.quotes = QuoteCache(
            max_age_seconds=config.market.max_quote_age_seconds, environment=self.environment
        )
        self.pauses = PauseManager(database.sessionmaker, self.environment)
        self.alerts = AlertManager(database.sessionmaker, self.environment)
        self.commands = ControlCommandManager(database.sessionmaker, self.environment)
        self.reservations = ReservationManager(self.environment)
        self.ledger = LedgerWriter(self.environment)
        self.scheduler = Scheduler(database.sessionmaker, self.environment, skip_grace_seconds=60)
        self.lease = ExecutorLease(
            database.engine, environment=self.environment, account_uid=self.uid
        )
        self.machine = RunStateMachine()
        self.specs = {}
        self.strategies = {}
        self.account = None
        self.positions = []
        self.account_at = None
        self.reconciled_at = None
        self.reconcile_ok = False
        self.identity_ok = False
        self.baseline_ok = False
        self.valuation = None
        self.daily_pnl = ZERO
        self.drawdown = ZERO
        self.stop_event = asyncio.Event()
        self.last_error = ""
        self._catalog_at = 0.0
        self._clock_at = 0.0
        self._last_wall = now_utc()
        self._last_mono = time.monotonic()
        self._config_hash = ""

    async def start(self):
        if self.environment == "live":
            gates = self.config.live_gates
            if not (gates.live_allowed and gates.acceptance_record_ref and gates.manual_enable_ref):
                raise RuntimeError("实盘门槛未满足（ENV-03），不能启动交易 worker")
            if self.config.ops.alert_channels == () or self.config.ops.risk_start_at is None:
                raise RuntimeError("实盘告警与初始风险基线尚未配置")
        status = await self.lease.try_acquire()
        if status.state is not LeaseState.NEEDS_CONFIRMATION:
            raise RuntimeError("已有执行者或数据库不可用，当前进程不接管")
        try:
            await self._adapters()
            self.executor = Executor(
                adapter=self.adapter,
                sessionmaker=self.database.sessionmaker,
                environment=self.environment,
                account_uid=self.uid,
                lease=self.lease,
                cancel_verify_timeout=self.config.execution.cancel_query_deadline_seconds,
            )
            self.reconciler = Reconciler(
                adapter=self.adapter,
                sessionmaker=self.database.sessionmaker,
                environment=self.environment,
                account_uid=self.uid,
                executor=self.executor,
            )
            await self._apply_config()
            await self._recover_inflight()
            async with self.database.sessionmaker() as session:
                await session.execute(
                    update(ScheduleRun)
                    .where(
                        ScheduleRun.environment == self.environment,
                        ScheduleRun.status == "RUNNING",
                    )
                    .values(status="FAILED", result={"reason": "worker interrupted; no replay"})
                )
                await session.commit()
            self.machine.transition(RunState.RECONCILING)
            await self.tick()
        except BaseException:
            await self.close()
            raise

    async def _adapters(self):
        if self.adapter is None:
            if self.environment == "paper":
                p = self.config.paper
                saved = await self.store.get("paper")
                self.adapter = self._paper_adapter(p)
                if saved:
                    try:
                        self.adapter.restore_state(saved)
                    except FeeModelMismatch as exc:
                        # 费用口径变了，历史模拟结果不可比，不能悄悄按新口径续算。
                        # 没有交易事实时才允许保留余额迁移费用元数据。
                        if (
                            await self._order_count()
                            or saved.get("orders")
                            or saved.get("executions")
                            or saved.get("settlements")
                            or any(Decimal(p["size"]) != 0 for p in saved.get("positions", []))
                        ):
                            raise RuntimeError(
                                "模拟检查点与当前费用口径不兼容且已有订单，禁止自动重置余额；"
                                "须人工审计后迁移"
                            ) from exc
                        # 未发生交易的检查点可迁移口径，但保留已有余额与审计副本。
                        await self.store.put("paper-fee-migration", {"previous": saved})
                        migrated = {
                            **saved,
                            "fees": json_value(asdict(self.config.fee_model)),
                        }
                        self.adapter.restore_state(migrated)
                        logger.warning("无交易模拟检查点的费用口径已迁移，原余额保持不变")
                        await self.adapter.checkpoint()
                else:
                    if await self._order_count():
                        raise RuntimeError("模拟检查点缺失但已有订单，禁止重置余额")
                    await self.adapter.checkpoint()
            else:
                profile = self.config.profile
                if not profile.allow_credentials or not profile.credential_ref:
                    raise RuntimeError("该环境尚未配置独立凭据")
                credentials = load_credentials(profile.credential_ref)
                self.adapter = BybitRestAdapter(
                    base_url=profile.base_url,
                    api_key=credentials.api_key,
                    api_secret=credentials.api_secret,
                    environment=self.environment,
                    allow_trade_writes=profile.allow_trade_writes,
                    proxy=self.config.runtime.proxy,
                    timeout=self.config.execution.request_timeout_seconds,
                )
                await self.adapter.__aenter__()
                self._owned_adapters.append(self.adapter)
        if self.market_adapter is None:
            if self.environment == "paper":
                self.market_adapter = BybitRestAdapter(
                    base_url=self.config.paper.public_market_url,
                    allow_trade_writes=False,
                    proxy=self.config.runtime.proxy,
                    timeout=10,
                    read_max_attempts=2,
                )
                await self.market_adapter.__aenter__()
                self._owned_adapters.append(self.market_adapter)
            else:
                self.market_adapter = self.adapter

    def _paper_adapter(self, paper_config) -> DurablePaperAdapter:
        """按当前费用口径构造模拟适配器（CR-02：模拟与实盘共用一套费率）。"""
        return DurablePaperAdapter(
            self.store,
            initial_balances=paper_config.initial_balances,
            account_uid=self.uid,
            fee_model=self.config.fee_model,
            account_mode=self.config.account.account_mode.value,
        )

    async def _order_count(self) -> int:
        async with self.database.sessionmaker() as session:
            return int(
                await session.scalar(
                    select(func.count())
                    .select_from(Order)
                    .where(Order.environment == self.environment)
                )
                or 0
            )

    def _fee_ratio(self, category: str, symbol: str, price: Decimal | None) -> Decimal:
        """费用占成交额的比例（预算校验与止盈判断共用）。

        期权按官方公式：``min(标的指数价 × 费率, 权利金 × 封顶比例)``。
        缺标的指数价时按封顶比例估算 —— 保守方向，宁可高估成本。
        """
        model = self.config.fee_model
        if category != "option":
            return model.spot_fee_ratio()
        if price is None or price <= 0:
            return model.option_premium_cap_rate
        index_price = None
        ticker = self.quotes.get_ticker("option", symbol)
        if ticker is not None and self.quotes.status("option", symbol).usable:
            index_price = ticker.index_price
        return model.option_fee_ratio(option_price=price, index_price=index_price)

    async def close(self):
        self.stop_event.set()
        with contextlib.suppress(Exception):
            self.machine = RunStateMachine(RunState.STOPPED)
            await self._status()
        with contextlib.suppress(Exception):
            await self.lease.release()
        for adapter in self._owned_adapters:
            await adapter.__aexit__(None, None, None)
        self._owned_adapters.clear()

    async def run(self):
        try:
            await self.start()
            while not self.stop_event.is_set():
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self.stop_event.wait(), self.config.runtime.poll_seconds)
                if self.stop_event.is_set():
                    break
                try:
                    await self.tick()
                except Exception as exc:
                    self.last_error = type(exc).__name__
                    logger.error("交易服务周期失败：%s；保持暂停，下一轮重新核对", self.last_error)
                    self.reconcile_ok = False
                    await self._fault("SERVICE_FAILURE", "服务周期失败，需要重新同步", locked=False)
                    with contextlib.suppress(Exception):
                        await self._status()
        finally:
            await self.close()

    async def _recover_inflight(self):
        async with self.database.sessionmaker() as session:
            commands = list(
                await session.scalars(
                    select(ControlCommand).where(
                        ControlCommand.environment == self.environment,
                        ControlCommand.status == CommandStatus.EXECUTING,
                    )
                )
            )
            for command in commands:
                command.status = CommandStatus.FAILED
                command.result = {"detail": "进程在执行期间终止，需核实原意图；不自动重放"}
            if commands:
                await session.commit()
        if commands:
            await self._fault(
                "CONTROL_INTERRUPTED", "控制命令执行期间重启，需人工核实", locked=True
            )

    async def _apply_config(self):
        fingerprint = config_fingerprint(self.config)
        old = await self.store.get("config")
        if old and old.get("fingerprint") != fingerprint:
            await self._fault(
                "CONFIG_CHANGED", "配置已变更，重新评估并撤销未完成开仓单", locked=True
            )
            if hasattr(self, "executor") and self.lease.state is LeaseState.ELIGIBLE:
                await self._cancel_open()
        async with self.database.sessionmaker() as session:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                {"key": f"{self.environment}:config"},
            )
            configured = {item.name for item in self.config.strategies}
            all_rows = list(
                await session.scalars(
                    select(Strategy).where(Strategy.environment == self.environment)
                )
            )
            for item in all_rows:
                if item.name not in configured:
                    item.enabled = False
            current = {}
            for cfg in self.config.strategies:
                row = next((item for item in all_rows if item.name == cfg.name), None)
                if row is None:
                    row = Strategy(
                        environment=self.environment,
                        name=cfg.name,
                        kind=cfg.kind,
                        enabled=cfg.enabled,
                    )
                    session.add(row)
                    await session.flush()
                row.enabled, row.kind = cfg.enabled, cfg.kind
                version = await session.scalar(
                    select(StrategyConfigVersion)
                    .where(StrategyConfigVersion.strategy_id == row.id)
                    .order_by(StrategyConfigVersion.version.desc())
                    .limit(1)
                )
                payload = cfg.model_dump(mode="json")
                if version is None or version.params != payload:
                    await session.execute(
                        update(StrategyConfigVersion)
                        .where(StrategyConfigVersion.strategy_id == row.id)
                        .values(active=False)
                    )
                    version = StrategyConfigVersion(
                        strategy_id=row.id,
                        version=(version.version + 1 if version else 1),
                        params=payload,
                        exit_rules=payload["exit_rules"],
                        symbols=list(cfg.symbols),
                        schedule_utc=list(cfg.schedule_utc),
                        budget_per_run=cfg.budget_per_run.value,
                        max_spend_per_trade=cfg.max_spend_per_trade.value,
                        currency=cfg.budget_per_run.currency,
                        active=True,
                        activated_at=now_utc(),
                    )
                    session.add(version)
                    await session.flush()
                current[row.id] = (cfg, version.version)
            await session.commit()
        self.strategies = current
        self._config_hash = fingerprint
        await self.store.put(
            "config",
            {
                "fingerprint": fingerprint,
                "config": self.config.model_dump(mode="json", exclude={"environments"}),
            },
        )

    async def tick(self):
        await self.lease.heartbeat()
        if self.lease.state in {LeaseState.LOST, LeaseState.HELD_BY_OTHER, LeaseState.FREE}:
            raise RuntimeError("执行资格失效，需要重启并重新确认接管")
        if self.config_path:
            updated = load_config(self.config_path)
            if (
                updated.profile != self.config.profile
                or updated.runtime.proxy != self.config.runtime.proxy
                or updated.fees != self.config.fees
            ):
                raise RuntimeError("环境、端点、凭据、代理或费用口径变更需重启")
            if config_fingerprint(updated) != self._config_hash:
                self.config = updated
                await self._apply_config()
        await self._clock_and_disk()
        await self._market()
        await self._account_sync()
        await self._process_commands()
        paused = (
            bool(await self.pauses.active_pauses()) or self.lease.state is not LeaseState.ELIGIBLE
        )
        target = RunState.PAUSED_OPEN if paused else RunState.RUNNING
        if self.machine.state != target:
            self.machine.transition(target)
        if self.lease.state is LeaseState.ELIGIBLE:
            await self._cancel_expired(paused=paused)
            await self._exit_rules()
            await self._schedule()
        await self._notifications()
        self.last_error = ""
        await self._status()

    async def _fault(self, code, detail, *, locked=None):
        await self.pauses.pause(reason_code=code, reason_detail=detail, locked=locked)
        await self.alerts.raise_alert(
            alert_key=code,
            severity="CRITICAL",
            title=detail,
            detail=detail,
            context={"environment": self.environment},
        )

    async def _clear_technical(self, code):
        await self.pauses.resolve_technical(reason_code=code)
        await self.alerts.resolve(code, detail="重新同步与校验通过")

    async def _clock_and_disk(self):
        wall, mono = now_utc(), time.monotonic()
        jump = abs((wall - self._last_wall).total_seconds() - (mono - self._last_mono))
        self._last_wall, self._last_mono = wall, mono
        if jump > self.config.ops.max_clock_skew_seconds:
            await self._fault("CLOCK_JUMP", "系统时钟发生跳变，需重新确认调度", locked=True)
        if mono - self._clock_at > 60:
            before = now_utc()
            remote = await self.market_adapter.get_server_time()
            after = now_utc()
            offset = abs((remote - (before + (after - before) / 2)).total_seconds())
            if offset > self.config.ops.max_clock_skew_seconds:
                await self._fault(PauseReason.CLOCK_SKEW, "本机时间与交易所偏差超限")
            else:
                await self._clear_technical(PauseReason.CLOCK_SKEW)
            self._clock_at = mono
        disk_path = Path("var") if await asyncio.to_thread(Path("var").exists) else Path.cwd()
        free = shutil.disk_usage(disk_path).free / 1024**3
        if free < self.config.ops.disk_pause_threshold_gb:
            await self._fault(PauseReason.DISK_PRESSURE, "磁盘空间低于暂停阈值")
        elif free < self.config.ops.disk_free_threshold_gb:
            await self.alerts.raise_alert(
                alert_key="DISK_WARNING",
                severity="WARNING",
                title="磁盘可用空间低于告警阈值",
                detail=f"{free:.1f} GiB",
            )
        else:
            await self._clear_technical(PauseReason.DISK_PRESSURE)

    async def _market(self):
        if (
            not self.specs
            or time.monotonic() - self._catalog_at > self.config.runtime.catalog_refresh_seconds
        ):
            await self.catalog.sync(self.market_adapter)
            self.specs = {
                (spec.category, spec.symbol): spec
                for category in ("spot", "option")
                for spec in await self.catalog.list(category)
            }
            self._catalog_at = time.monotonic()
            if isinstance(self.adapter, PaperAdapter):
                for spec in self.specs.values():
                    self.adapter.set_instrument(spec)
        needed = {
            ("spot", symbol)
            for cfg, _ in self.strategies.values()
            if cfg.kind == "spot_fixed_budget_dca"
            for symbol in cfg.symbols
        }
        option_bases = {
            base
            for cfg, _ in self.strategies.values()
            if cfg.enabled and cfg.kind == "option_single_leg_buy"
            for base in cfg.symbols
        }
        async with self.database.sessionmaker() as session:
            orders = list(
                await session.scalars(
                    select(Order).where(
                        Order.environment == self.environment, Order.status.notin_(TERMINAL_STATES)
                    )
                )
            )
            inventory = list(
                await session.scalars(
                    select(Inventory).where(
                        Inventory.environment == self.environment, Inventory.qty > 0
                    )
                )
            )
        needed.update((order.category, order.symbol) for order in orders)
        for item in inventory:
            needed.add(("option" if "-" in item.symbol else "spot", item.symbol))
        for base in option_bases:
            needed.add(("spot", f"{base}{self.config.ops.valuation.currency}"))
        for category, symbol in sorted(needed):
            await self._quote(category, symbol, depth=True)
        option_specs = [
            spec
            for (kind, _), spec in self.specs.items()
            if kind == "option"
            and spec.base_coin in option_bases
            and spec.is_tradable
            and spec.delivery_time
            and self.config.option.min_days_to_expiry
            <= (spec.delivery_time - now_utc()).total_seconds() / 86400
            <= self.config.option.max_days_to_expiry
        ]
        option_specs.sort(key=lambda spec: (spec.delivery_time, spec.symbol))
        allowed = {
            spec.symbol for spec in option_specs[: self.config.runtime.max_option_candidates]
        }
        if option_specs:
            for base in sorted(option_bases):
                for ticker in await self.market_adapter.get_tickers("option", base_coin=base):
                    if ticker.symbol in allowed:
                        self.quotes.put_ticker(ticker)
                        if isinstance(self.adapter, PaperAdapter):
                            self.adapter.set_ticker(ticker)
        if isinstance(self.adapter, DurablePaperAdapter):
            await self.adapter.checkpoint()

    async def _quote(self, category, symbol, *, depth=False):
        ticker = await self.market_adapter.get_ticker(category, symbol)
        self.quotes.put_ticker(ticker)
        if isinstance(self.adapter, PaperAdapter):
            self.adapter.set_ticker(ticker)
        if depth:
            book = await self.market_adapter.get_orderbook(category, symbol)
            self.quotes.put_book(book)
            if (
                isinstance(self.adapter, PaperAdapter)
                and self.quotes.status(category, symbol, require_greeks=category == "option").usable
            ):
                self.adapter.set_book(book)
                if isinstance(self.adapter, DurablePaperAdapter):
                    await self.adapter.checkpoint()
        return ticker

    async def _paper_settlements(self):
        if not isinstance(self.adapter, PaperAdapter):
            return
        for event in self.adapter.settlements():
            async with self.database.sessionmaker() as session:
                done = await session.scalar(
                    select(LedgerEvent.id).where(
                        LedgerEvent.environment == self.environment,
                        LedgerEvent.dedupe_key == f"settlement:{event['settlement_id']}",
                    )
                )
                if done is not None:
                    continue
                inventory = list(
                    await session.scalars(
                        select(Inventory)
                        .where(
                            Inventory.environment == self.environment,
                            Inventory.symbol == event["symbol"],
                            Inventory.qty > 0,
                        )
                        .order_by(Inventory.strategy_id)
                    )
                )
                qty = sum((row.qty for row in inventory), ZERO)
                positions = await self.adapter.get_positions("option")
                if qty != Decimal(event["qty"]) or any(
                    p.symbol == event["symbol"] and p.size != 0 for p in positions
                ):
                    raise RuntimeError("模拟交割数量与归属/实际持仓不符")
                allocations = []
                payout_left, fee_left = Decimal(event["payout"]), Decimal(event["fee"])
                for index, row in enumerate(inventory):
                    payout = (
                        payout_left
                        if index == len(inventory) - 1
                        else Decimal(event["payout"]) * row.qty / qty
                    )
                    fee = (
                        fee_left
                        if index == len(inventory) - 1
                        else Decimal(event["fee"]) * row.qty / qty
                    )
                    payout_left -= payout
                    fee_left -= fee
                    allocations.append(
                        {
                            "strategy_id": row.strategy_id,
                            "qty": str(row.qty),
                            "payout": str(payout),
                            "fee": str(fee),
                        }
                    )
                await self.ledger.record_option_settlement(
                    session,
                    settlement_id=event["settlement_id"],
                    symbol=event["symbol"],
                    currency=event["currency"],
                    occurred_at=dt.datetime.fromisoformat(event["occurred_at"]),
                    allocations=allocations,
                    evidence={
                        "source": "paper",
                        "reference": event["reference"],
                        "balance_reconciled": True,
                        "positions_reconciled": True,
                        "settlement_price": event["settlement_price"],
                    },
                )
                await session.commit()

    async def _account_sync(self):
        await self._paper_settlements()
        result = await self.reconciler.reconcile()
        self.reconcile_ok, self.reconciled_at = result.allows_resume, now_utc()
        if not result.allows_resume:
            await self._fault(
                PauseReason.RECONCILIATION_DIFFERENCE, "订单、成交、持仓或预留存在待核实差异"
            )
        self.account = await self.adapter.get_account()
        self.account_at = now_utc()
        self.positions = await self.adapter.get_positions("option")
        key = await self.adapter.get_key_info()
        is_paper = self.environment == "paper"
        self.identity_ok = (
            self.account.account_uid == self.uid
            and self.account.account_mode.lower() == self.config.account.account_mode.value
            and not key.withdraw_enabled
        )
        if not is_paper:
            required = {
                "SpotTrade" if product.value == "spot" else "OptionsTrade"
                for product in self.config.account.allowed_products
            }
            self.identity_ok = (
                self.identity_ok
                and key.account_uid == self.uid
                and required.issubset(key.permissions)
                and not key.read_only
            )
            if self.config.account.bound_ip:
                self.identity_ok = (
                    self.identity_ok and self.config.account.bound_ip in key.ip_restrictions
                )
        if not self.identity_ok:
            await self._fault(PauseReason.ACCOUNT_UNTRUSTED, "账户身份、模式或权限校验未通过")
        if self.account.has_debt or any(position.side == "Sell" for position in self.positions):
            await self._fault(PauseReason.ACCOUNT_DEBT, "发现非预期负债或期权空头")
        async with self.database.sessionmaker() as session:
            session.add(
                AccountSnapshot(
                    environment=self.environment,
                    account_uid=self.account.account_uid,
                    account_mode=self.account.account_mode,
                    total_equity=self.account.total_equity,
                    coins=json_value(
                        {coin: asdict(balance) for coin, balance in self.account.coins.items()}
                    ),
                    raw={},
                    complete=self.identity_ok and self.reconcile_ok,
                    captured_at=self.account_at,
                )
            )
            await session.commit()
        await self._value()
        if self.identity_ok and self.reconcile_ok:
            await self._clear_technical("SERVICE_FAILURE")

    async def _value(self):
        currency = self.config.ops.valuation.currency
        rates = {currency: Decimal("1")}
        for coin, balance in self.account.coins.items():
            if coin == currency or balance.wallet_balance == 0:
                continue
            symbol = f"{coin}{currency}"
            if ("spot", symbol) in self.specs:
                await self._quote("spot", symbol)
                quote = self.quotes.get_ticker("spot", symbol)
                if self.quotes.status("spot", symbol).usable:
                    rates[coin] = quote.bid_price
        positions = []
        for position in self.positions:
            ticker = await self._quote("option", position.symbol)
            spec = self.specs.get(("option", position.symbol))
            if not self.quotes.status("option", position.symbol).usable or not spec:
                positions.append(replace(position, mark_price=None))
            else:
                price = (
                    ticker.mark_price
                    if self.config.ops.valuation.option_valuation_source == "mark"
                    else (ticker.bid_price)
                )
                positions.append(
                    replace(
                        position,
                        mark_price=price,
                        settle_coin=spec.settle_coin,
                        delivery_time=spec.delivery_time,
                    )
                )
        policy = self.config.ops.valuation
        self.valuation = value_account(
            ValuationPolicy(
                version=policy.version,
                currency=currency,
                day_boundary_tz=self.config.ops.day_boundary_tz,
            ),
            ValuationInput(
                account=self.account,
                cash_rates=rates,
                option_positions=positions,
                settle_rates=rates,
            ),
        )
        if (
            self.positions
            and self.environment != "paper"
            and not self.config.runtime.option_units_verified
        ):
            self.valuation = replace(
                self.valuation,
                complete=False,
                incomplete_reason="期权数量和估值单位未经实际账户验证",
            )
        self._rates = rates
        self._valued_positions = positions
        if not self.valuation.complete:
            self.baseline_ok = False
            await self._fault(PauseReason.VALUATION_UNAVAILABLE, self.valuation.incomplete_reason)
            return
        await self._baseline()
        if self.baseline_ok:
            await self._clear_technical(PauseReason.VALUATION_UNAVAILABLE)
        else:
            await self._fault(
                PauseReason.VALUATION_UNAVAILABLE, "日初或首次风险基线缺失，需要明确初始化"
            )
        if self.daily_pnl <= -self.config.risk.max_daily_loss.value and self.baseline_ok:
            await self._fault(PauseReason.LOSS_CIRCUIT_BREAKER, "当日资金流调整亏损达到限额")
        if self.drawdown >= self.config.risk.max_drawdown.value and self.baseline_ok:
            await self._fault(PauseReason.DRAWDOWN_LIMIT, "资金流调整权益回撤达到限额")

    async def _baseline(self):
        moment = now_utc()
        day = local_day(moment, self.config.ops.day_boundary_tz)
        currency = self.valuation.currency
        version = self.valuation.policy_version
        async with self.database.sessionmaker() as session:
            flows = list(
                await session.scalars(
                    select(CashFlow).where(
                        CashFlow.environment == self.environment, CashFlow.external.is_(True)
                    )
                )
            )
            if any(flow.asset != currency for flow in flows):
                self.baseline_ok = False
                return
            total_flow = sum((flow.amount for flow in flows), ZERO)
            today_flow = sum(
                (
                    flow.amount
                    for flow in flows
                    if flow.occurred_at
                    >= (local_day_start_utc(moment, self.config.ops.day_boundary_tz))
                ),
                ZERO,
            )
            previous = await session.scalar(
                select(EquityBaseline)
                .where(EquityBaseline.environment == self.environment)
                .order_by(EquityBaseline.established_at.desc())
                .limit(1)
            )
            baseline = await session.scalar(
                select(EquityBaseline).where(
                    EquityBaseline.environment == self.environment, EquityBaseline.day == day
                )
            )
            adjusted = self.valuation.equity - total_flow
            if (
                baseline is None
                and previous is not None
                and (previous.policy_version == version and previous.currency == currency)
            ):
                boundary = local_day_start_utc(moment, self.config.ops.day_boundary_tz)
                prior = await session.scalar(
                    select(EquitySnapshot)
                    .where(
                        EquitySnapshot.environment == self.environment,
                        EquitySnapshot.policy_version == version,
                        EquitySnapshot.valuation_complete.is_(True),
                        EquitySnapshot.at <= boundary,
                        EquitySnapshot.at >= boundary - dt.timedelta(seconds=15),
                    )
                    .order_by(EquitySnapshot.at.desc())
                    .limit(1)
                )
                # 无对应时点价格时只重建纯现金、期间无交易及资金流的常量权益。
                # 有风险资产时，靠近日界线的旧快照不能冒充日初价格。
                cash_only = not self.positions and all(
                    coin == currency or balance.wallet_balance == ZERO
                    for coin, balance in self.account.coins.items()
                )
                quiet = False
                if prior is not None and cash_only and prior.equity == self.valuation.equity:
                    fills = await session.scalar(
                        select(func.count())
                        .select_from(Execution)
                        .where(
                            Execution.environment == self.environment,
                            Execution.exec_time >= prior.at,
                        )
                    )
                    quiet = not fills and not any(flow.occurred_at >= prior.at for flow in flows)
                if prior is not None and (prior.at == boundary or quiet):
                    baseline = EquityBaseline(
                        environment=self.environment,
                        day=day,
                        equity=prior.equity,
                        currency=currency,
                        policy_version=version,
                        complete=True,
                        drawdown_peak=previous.drawdown_peak,
                        established_at=boundary,
                    )
                    session.add(baseline)
            self.baseline_ok = (
                baseline is not None
                and baseline.policy_version == version
                and (baseline.currency == currency)
            )
            if self.baseline_ok:
                baseline.drawdown_peak = max(baseline.drawdown_peak, adjusted)
                self.daily_pnl = self.valuation.equity - baseline.equity - today_flow
                self.drawdown = max(ZERO, baseline.drawdown_peak - adjusted)
            session.add(
                EquitySnapshot(
                    environment=self.environment,
                    at=moment,
                    equity=self.valuation.equity,
                    currency=currency,
                    policy_version=version,
                    net_external_flow=total_flow,
                    adjusted_equity=adjusted,
                    valuation_complete=True,
                    details={**self.valuation.details, "baseline_available": self.baseline_ok},
                )
            )
            await session.commit()

    async def establish_baseline(self, *, operator: str):
        if not self.valuation or not self.valuation.complete or not self.reconcile_ok:
            raise RuntimeError("必须先取得完整估值并完成对账")
        async with self.database.sessionmaker() as session:
            existing = await session.scalar(
                select(func.count())
                .select_from(EquityBaseline)
                .where(EquityBaseline.environment == self.environment)
            )
            if existing:
                raise RuntimeError("已有风险起点，不能以重新初始化清除历史")
            moment = now_utc()
            session.add(
                EquityBaseline(
                    environment=self.environment,
                    day=local_day(moment, self.config.ops.day_boundary_tz),
                    equity=self.valuation.equity,
                    currency=self.valuation.currency,
                    policy_version=self.valuation.policy_version,
                    complete=False,
                    drawdown_peak=self.valuation.equity,
                    established_at=moment,
                )
            )
            await self.ledger.append_event(
                session,
                event_type="RISK_START",
                dedupe_key="risk-start",
                payload={
                    "category": "account",
                    "operator": operator,
                    "at": moment.isoformat(),
                    "equity": str(self.valuation.equity),
                    "incomplete_day": True,
                },
                occurred_at=moment,
            )
            await session.commit()
        self.baseline_ok = True

    async def _process_commands(self):
        for command in await self.commands.pending():
            await self.commands.execute(
                command,
                executor=self.executor,
                resume_handler=self._resume,
                reduce_handler=self._reduce,
                takeover_handler=self._takeover,
                baseline_handler=self._initialize_baseline,
                cash_flow_handler=self._register_external_cash_flow,
            )

    async def _register_external_cash_flow(self, command):
        """登记外部充值/提现/划转（LED-03 / LED-04）。

        账户外的资金变化必须显式登记：未登记的现金变化会被判为
        ``EXTERNAL_CASH_CHANGE`` 并触发 CRITICAL 暂停；登记后外部资金计入
        账户总风险但不归属任何策略，日亏损与回撤按净流量调整。
        """
        if self.environment == "paper":
            raise RuntimeError("模拟环境不登记外部资金流")
        if not self.identity_ok:
            raise RuntimeError("账户身份未通过验证，不能登记外部资金流")
        payload = command.payload or {}
        currency = self.config.ops.valuation.currency.upper()
        asset = str(payload.get("asset", "")).strip().upper()
        if asset != currency:
            raise ValueError(f"只能登记估值币种 {currency} 的外部资金流")
        kind = str(payload.get("kind", ""))
        sign = self._EXTERNAL_FLOW_SIGNS.get(kind)
        if sign is None:
            raise ValueError("未知的外部资金流类型")
        reference = str(payload.get("reference", "")).strip()
        if not reference:
            raise ValueError("必须填写交易所侧的资金流水标识")
        try:
            amount = Decimal(str(payload.get("amount")))
        except (InvalidOperation, ValueError, TypeError):
            raise ValueError("金额无法解析") from None
        if not amount.is_finite() or amount <= 0:
            raise ValueError("金额必须为正数，资金方向由类型决定")
        occurred_at = now_utc()
        raw_time = payload.get("occurred_at")
        if raw_time:
            try:
                parsed = dt.datetime.fromisoformat(str(raw_time))
            except ValueError:
                raise ValueError("资金发生时间无法解析") from None
            if parsed.tzinfo is None:
                raise ValueError("资金发生时间必须带时区")
            if parsed > occurred_at + dt.timedelta(minutes=5):
                raise ValueError("资金发生时间不能晚于当前时间")
            occurred_at = parsed
        signed = amount * sign
        # 以交易所流水标识去重：同一笔划转不能被两条命令重复登记。
        dedupe_key = f"external-cash-flow:{reference}"[:256]
        async with self.database.sessionmaker() as session:
            flow = await self.ledger.record_cash_flow(
                session,
                asset=asset,
                amount=signed,
                kind=kind,
                dedupe_key=dedupe_key,
                occurred_at=occurred_at,
                external=True,
            )
            if flow is None:
                raise RuntimeError("该笔资金流已经登记过")
            await self.ledger.append_event(
                session,
                event_type="REGISTER_EXTERNAL_CASH_FLOW",
                dedupe_key=f"audit:external-cash-flow:{command.command_id}",
                payload={
                    "category": "cash_flow",
                    "operator": command.requested_by,
                    "command_id": command.command_id,
                    "cash_flow_id": flow.id,
                    "asset": asset,
                    "amount": str(signed),
                    "kind": kind,
                    "reference": reference,
                    "reason": str(payload.get("reason", "")),
                },
                occurred_at=occurred_at,
            )
            await session.commit()
        return {
            "cash_flow_id": flow.id,
            "asset": asset,
            "amount": str(signed),
            "kind": kind,
            "reference": reference,
            "external": True,
        }

    async def _initialize_baseline(self, command):
        if not self.identity_ok:
            raise RuntimeError("账户身份未通过验证")
        await self.establish_baseline(operator=command.requested_by)
        return {"baseline_created": True, "incomplete_day": True}

    async def _takeover(self, command):
        if (
            not self.identity_ok
            or not self.reconcile_ok
            or not self.reconciled_at
            or (now_utc() - self.reconciled_at).total_seconds() > 30
        ):
            raise RuntimeError("身份或对账尚未通过，不能确认接管")
        if not command.payload.get("evidence"):
            raise ValueError("需明确确认旧执行者已停止的依据")
        await self.lease.confirm_takeover(
            operator=command.requested_by, evidence=command.payload["evidence"]
        )
        return {"lease": self.lease.state.value, "reconciled_at": self.reconciled_at.isoformat()}

    async def _resume(self, command):
        if (
            not self.identity_ok
            or not self.reconcile_ok
            or not self.baseline_ok
            or (self.lease.state is not LeaseState.ELIGIBLE)
            or not self.valuation.complete
        ):
            raise RuntimeError("身份、执行权、估值基线或对账条件尚未满足")
        if self.account.has_debt:
            raise RuntimeError("负债尚未消除")
        async with self.database.sessionmaker() as session:
            unknown = await session.scalar(
                select(func.count())
                .select_from(Order)
                .where(
                    Order.environment == self.environment,
                    Order.status.in_(["UNKNOWN", "SUBMITTING"]),
                )
            )
            pause = await session.get(RiskPause, int(command.payload["pause_id"]))
        if pause is None or pause.environment != self.environment:
            raise ValueError("暂停记录不存在")
        if unknown:
            raise RuntimeError("未知订单尚未由交易事实核实；人工确认不能替代证据")
        if pause.reason_code == PauseReason.LOSS_CIRCUIT_BREAKER and (
            -self.daily_pnl >= self.config.risk.max_daily_loss.value
        ):
            raise RuntimeError("当前日亏损仍超限")
        if pause.reason_code == PauseReason.DRAWDOWN_LIMIT and (
            self.drawdown >= self.config.risk.max_drawdown.value
        ):
            raise RuntimeError("当前回撤仍超限")
        await self.pauses.resolve(
            pause_id=pause.id,
            operator=command.requested_by,
            reason=command.payload["reason"],
            evidence={
                "command_id": command.command_id,
                "reconciliation_at": self.reconciled_at.isoformat(),
            },
        )
        return {
            "resolved_pause_id": pause.id,
            "remaining_pauses": len(await self.pauses.active_pauses()),
        }

    async def _reduce(self, command):
        p = command.payload
        strategy_id = int(p["strategy_id"])
        if strategy_id not in self.strategies:
            raise ValueError("策略不属于当前环境")
        spec = self.specs.get((p["category"], p["symbol"]))
        if spec is None:
            raise ValueError("交易规格缺失")
        price = Decimal(p["price_floor"])
        qty = Decimal(p["qty"])
        draft = IntentDraft(
            action="OPTION_SELL_CLOSE" if spec.category == "option" else "SPOT_SELL",
            category=spec.category,
            symbol=spec.symbol,
            side="Sell",
            qty=qty,
            qty_unit="contract" if spec.category == "option" else "base_coin",
            currency=spec.settle_coin or spec.quote_coin,
            spend_limit=ZERO,
            price_limit=price,
            reduce_qty=qty,
            underlying=spec.base_coin,
            ttl_seconds=60,
        )
        return await self._submit_draft(
            strategy_id,
            self.strategies[strategy_id][1],
            draft,
            planned_at=now_utc(),
            signal=f"control:{command.command_id}",
        )

    def _scheduled_strategy(self, cfg, symbol):
        cap = min(cfg.budget_per_run.value / len(cfg.symbols), cfg.max_spend_per_trade.value)
        params = cfg.params
        if cfg.kind == "spot_fixed_budget_dca":
            return ScheduledSpotBuyStrategy(
                cfg.name,
                SpotBuyParams(
                    symbol=symbol,
                    budget_per_run=cap,
                    currency=cfg.budget_per_run.currency,
                    enabled=True,
                    ttl_seconds=cfg.signal_ttl_seconds,
                    fee_rate=self.config.fee_model.spot_fee_ratio(),
                    max_price_deviation=Decimal(params.get("max_price_deviation", "0.005")),
                    min_qty=Decimal(params.get("min_qty", "0")),
                ),
            )
        target = self.config.option.delta_range
        strike = self.config.option.strike_range_pct
        return ScheduledOptionBuyStrategy(
            cfg.name,
            OptionBuyParams(
                underlying=symbol,
                currency=cfg.budget_per_run.currency,
                allowed_types=self.config.option.allowed_option_types,
                options_type=params.get("options_type"),
                min_days_to_expiry=self.config.option.min_days_to_expiry,
                max_days_to_expiry=self.config.option.max_days_to_expiry,
                target_delta=(sum(target) / 2 if target else None),
                delta_tolerance=((target[1] - target[0]) / 2 if target else Decimal("0.25")),
                strike_range=tuple(value / 100 for value in strike) if strike else None,
                min_premium=Decimal(params.get("min_premium", "0")),
                max_premium=Decimal(params.get("max_premium", "0")),
                min_open_interest=Decimal(params.get("min_open_interest", "0")),
                premium_budget=cap,
                enabled=True,
                ttl_seconds=cfg.signal_ttl_seconds,
                fee_model=self.config.fee_model,
                max_spread_ratio=self.config.market.max_spread_bps / 10000,
            ),
        )

    async def _schedule(self):
        now = now_utc()
        for strategy_id, (cfg, version) in self.strategies.items():
            if not cfg.enabled:
                continue
            for symbol in cfg.symbols:
                for scheduled in cfg.schedule_utc:
                    hour, minute = map(int, scheduled.split(":"))
                    planned = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                    if planned <= now:
                        await self.scheduler.plan(
                            strategy_id=strategy_id,
                            strategy_version=version,
                            rule_id=f"{symbol}:{scheduled}",
                            planned_at=planned,
                            input_snapshot={"timezone": "UTC", "symbol": symbol},
                        )
        for run in await self.scheduler.claim_due():
            try:
                result = {"skipped": True, "reason": "disabled, paused or version changed"}
                cfg, version = self.strategies.get(run.strategy_id, (None, None))
                if (
                    cfg is not None
                    and cfg.enabled
                    and version == run.strategy_version
                    and self.machine.allows_new_exposure
                    and not await self.scheduler.in_cooldown(
                        strategy_id=run.strategy_id,
                        rule_id=run.rule_id,
                        cooldown_seconds=cfg.cooldown_seconds,
                    )
                ):
                    strategy = self._scheduled_strategy(cfg, run.input_snapshot["symbol"])
                    valid = {
                        key: spec
                        for key, spec in self.specs.items()
                        if self.quotes.status(*key, require_greeks=key[0] == "option").usable
                    }
                    context = SignalContext(
                        environment=self.environment,
                        account_uid=self.uid,
                        strategy_id=run.strategy_id,
                        strategy_version=version,
                        rule_id=run.rule_id,
                        planned_at=run.planned_at,
                    )
                    result = {"skipped": True, "reason": "no eligible candidate"}
                    for draft in strategy.evaluate(context, DecisionView(valid, self.quotes, now)):
                        draft = replace(
                            draft,
                            exit_rules=tuple(
                                rule.model_dump(mode="json") for rule in cfg.exit_rules
                            ),
                        )
                        result = await self._submit_draft(
                            run.strategy_id,
                            version,
                            draft,
                            planned_at=run.planned_at,
                            rule_id=run.rule_id,
                        )
                status = (
                    "SKIPPED"
                    if result.get("skipped")
                    else ("REJECTED" if result.get("status") == "REJECTED" else "DONE")
                )
                await self.scheduler.finish(run.id, status=status, result=result)
            except Exception as exc:
                await self.scheduler.finish(
                    run.id, status="FAILED", result={"error": type(exc).__name__}
                )
                await self._fault("SERVICE_FAILURE", "调度执行失败，原任务不自动重放", locked=False)

    async def _submit_draft(
        self,
        strategy_id,
        version,
        draft,
        *,
        planned_at,
        rule_id="manual",
        signal=None,
        order_price=None,
        order_ttl_seconds=None,
    ):
        if (
            draft.price_limit is None
            or not draft.price_limit.is_finite()
            or draft.price_limit <= 0
            or not draft.qty.is_finite()
            or draft.qty <= 0
        ):
            raise ValueError("订单必须包含有限正数的限价和数量")
        physical_price = draft.price_limit if order_price is None else order_price
        if (
            not physical_price.is_finite()
            or physical_price <= 0
            or (draft.side == "Buy" and physical_price > draft.price_limit)
            or (draft.side == "Sell" and physical_price < draft.price_limit)
        ):
            raise ValueError("物理订单价格突破意图边界")
        # 每个授权重新取得账户与成交事实，避免上一笔成交后沿用旧余额。
        await self._account_sync()
        ticker = await self._quote(draft.category, draft.symbol, depth=True)
        spec = self.specs.get((draft.category, draft.symbol))
        if spec is None or not spec.qty_step or not spec.tick_size:
            raise ValueError("缺少交易规格，拒绝订单")
        if (
            draft.category == "option"
            and self.environment != "paper"
            and not (
                self.config.runtime.option_units_verified
                and self.config.runtime.option_reduce_only_verified
            )
        ):
            raise ValueError("期权真实数量与 reduceOnly 能力未经验证")
        snapshot = await self.quotes.persist_snapshot(
            self.database.sessionmaker,
            draft.category,
            draft.symbol,
            ticker,
            book=self.quotes.get_book(draft.category, draft.symbol),
        )
        ctx = SignalContext(
            environment=self.environment,
            account_uid=self.uid,
            strategy_id=strategy_id,
            strategy_version=version,
            rule_id=rule_id,
            planned_at=planned_at,
            quote_snapshot_id=snapshot,
        )
        intent = build_intent(ctx, draft)
        if signal:
            intent.signal_id = signal
        opening = draft.action in OPEN_ACTIONS
        async with self.database.sessionmaker() as session:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                {"key": f"{self.environment}:account-authorization"},
            )
            existing = await session.scalar(
                select(TradeIntent).where(
                    TradeIntent.environment == self.environment,
                    TradeIntent.signal_id == intent.signal_id,
                )
            )
            if existing:
                return {"intent_id": existing.id, "status": existing.status, "duplicate": True}
            session.add(intent)
            await session.flush()
            risk_context = await self._risk_context(session, intent)
            evaluation = await RiskEngine(self.config).evaluate(intent, risk_context)
            extra = []
            cfg = self.strategies[strategy_id][0]
            allowed = (
                draft.symbol in cfg.symbols
                if draft.category == "spot"
                else spec.base_coin in cfg.symbols
            )
            if not allowed or spec.category not in {
                product.value for product in self.config.account.allowed_products
            }:
                extra.append("标的不在该策略或账户白名单")
            if opening and (not spec.is_tradable or not self.machine.allows_new_exposure):
                extra.append("暂停新增风险或合约不可交易")
            if draft.price_limit is None or draft.qty <= 0 or draft.price_limit <= 0:
                extra.append("数量与限价必须为正数")
            if (
                draft.qty % spec.qty_step != 0
                or draft.price_limit % spec.tick_size != 0
                or physical_price % spec.tick_size != 0
            ):
                extra.append("数量或价格未对齐最新步长")
            if spec.min_order_qty and draft.qty < spec.min_order_qty:
                extra.append("低于最小数量")
            if spec.max_order_qty and draft.qty > spec.max_order_qty:
                extra.append("超过最大数量")
            if spec.min_order_amt and draft.qty * draft.price_limit < spec.min_order_amt:
                extra.append("低于最小订单金额")
            fee_rate = self._fee_ratio(draft.category, draft.symbol, draft.price_limit)
            if opening and draft.qty * draft.price_limit * (1 + fee_rate) > draft.spend_limit:
                extra.append("预算不足以支付价格上限与费用")
            if not self.quotes.status(
                draft.category, draft.symbol, require_greeks=draft.category == "option"
            ).usable:
                extra.append("行情不完整或已过期")
            reservation = None
            if evaluation.approved and not extra:
                try:
                    async with session.begin_nested():
                        if opening:
                            reservation = await self.reservations.reserve_cash(
                                session,
                                intent_id=intent.id,
                                amount=draft.spend_limit + (self.config.budget.fee_buffer.value),
                                currency=draft.currency,
                                account=self.account,
                            )
                        else:
                            free_qty = None
                            reflected = ()
                            if draft.category == "spot":
                                balance = self.account.coin(spec.base_coin)
                                available = balance.wallet_balance
                                if available is None:
                                    raise ValueError("现货自有库存快照缺失")
                                free_qty = balance.available
                                reflected = self.account.raw.get("reflected_order_link_ids", ())
                            else:
                                available = sum(
                                    (
                                        p.size
                                        for p in self.positions
                                        if p.symbol == draft.symbol and p.side == "Buy"
                                    ),
                                    ZERO,
                                )
                            reservation = await self.reservations.reserve_inventory(
                                session,
                                intent_id=intent.id,
                                symbol=draft.symbol,
                                qty=draft.qty,
                                account_qty=available,
                                account_available=free_qty,
                                reflected_order_link_ids=reflected,
                                account_trusted=self.identity_ok and self.reconcile_ok,
                                strategy_id=strategy_id,
                            )
                except ReservationError as exc:
                    extra.append(f"{exc.code}: {exc}")
            approved = evaluation.approved and not extra
            intent.status = "APPROVED" if approved else "REJECTED"
            session.add(
                RiskDecision(
                    intent_id=intent.id,
                    decision=intent.status,
                    reason_code=evaluation.reason_code if not extra else "VALIDATION_REJECTED",
                    reason_detail="; ".join(extra) or evaluation.reason_detail,
                    config_version=self._config_hash,
                    checks=[check.as_dict() for check in evaluation.checks],
                )
            )
            await self.ledger.append_event(
                session,
                event_type="INTENT",
                dedupe_key=f"intent:{intent.signal_id}",
                payload={
                    "category": intent.category,
                    "intent_id": intent.id,
                    "rationale": draft.rationale,
                    "status": intent.status,
                },
                occurred_at=now_utc(),
            )
            await session.commit()
        if not approved:
            return {
                "intent_id": intent.id,
                "status": "REJECTED",
                "reason": "; ".join(extra) or (evaluation.reason_detail),
            }
        ttl = now_utc() + dt.timedelta(
            seconds=(
                self.config.execution.order_ttl_seconds
                if order_ttl_seconds is None
                else order_ttl_seconds
            )
        )
        if not opening:
            ttl = min(ttl, intent.expires_at)
        if draft.category == "option" and opening:
            start = self.config.option.no_open_window_start_hour_utc
            end = self.config.option.no_open_window_end_hour_utc
            if start != end:
                boundary = now_utc().replace(hour=start, minute=0, second=0, microsecond=0)
                if boundary <= now_utc():
                    boundary += dt.timedelta(days=1)
                ttl = min(ttl, boundary)
        outcome = await self.executor.submit(
            OrderPlan(
                intent_id=intent.id,
                category=intent.category,
                symbol=intent.symbol,
                side=intent.side,
                qty=intent.qty,
                price=physical_price,
                reduce_only=draft.category == "option" and not opening,
                position_idx=0 if draft.category == "option" else None,
                strategy_id=strategy_id,
                spend_limit=intent.spend_limit,
                reservation_id=reservation.id,
                local_ttl_expires_at=ttl,
            )
        )
        if outcome.is_unknown:
            await self._fault(PauseReason.UNKNOWN_ORDER, "提交结果未知，保留预留并核实原订单")
        return {
            "intent_id": intent.id,
            "status": outcome.outcome.value,
            "order_link_id": outcome.order.order_link_id if outcome.order else None,
        }

    async def _risk_context(self, session, intent):
        orders = list(
            await session.scalars(
                select(Order).where(
                    Order.environment == self.environment, Order.status.notin_(TERMINAL_STATES)
                )
            )
        )
        inventory = list(
            await session.scalars(
                select(Inventory).where(Inventory.environment == self.environment)
            )
        )
        # 查询成对记录时使用 execute，不依赖 scalar 把归属信息截掉。
        held_pairs = (
            await session.execute(
                select(Reservation, TradeIntent)
                .join(TradeIntent, TradeIntent.id == Reservation.intent_id)
                .where(
                    Reservation.environment == self.environment,
                    Reservation.kind == "CASH",
                    Reservation.status != "RELEASED",
                )
            )
        ).all()
        premium = ZERO
        by_base, by_expiry = {}, {}
        strategy_spot, account_spot = ZERO, ZERO
        for item in inventory:
            spec = self.specs.get(("option" if "-" in item.symbol else "spot", item.symbol))
            if spec is None:
                continue
            cost = item.qty * item.avg_cost
            if spec.category == "option":
                premium += cost
                by_base[spec.base_coin] = by_base.get(spec.base_coin, ZERO) + cost
                key = (
                    f"{spec.base_coin}:{spec.delivery_time:%Y-%m-%d}"
                    if spec.delivery_time
                    else "unknown"
                )
                by_expiry[key] = by_expiry.get(key, ZERO) + cost
            elif item.strategy_id == intent.strategy_id:
                rate = self._rates.get(spec.base_coin)
                if rate is not None:
                    strategy_spot += item.qty * rate
        for coin, balance in self.account.coins.items():
            if coin in self.config.risk.delta_limit_per_underlying and coin in self._rates:
                account_spot += (balance.wallet_balance or ZERO) * self._rates[coin]
        for reservation, pending in held_pairs:
            remainder = max(ZERO, reservation.amount - reservation.consumed - reservation.released)
            if pending.category == "option":
                premium += remainder
                base = pending.underlying or pending.symbol.split("-")[0]
                by_base[base] = by_base.get(base, ZERO) + remainder
                key = (
                    f"{base}:{pending.delivery_time:%Y-%m-%d}"
                    if pending.delivery_time
                    else "unknown"
                )
                by_expiry[key] = by_expiry.get(key, ZERO) + remainder
            else:
                account_spot += remainder
                if pending.strategy_id == intent.strategy_id:
                    strategy_spot += remainder
        exposures = []
        delta = {
            coin: balance.wallet_balance or ZERO
            for coin, balance in self.account.coins.items()
            if coin in self.config.risk.delta_limit_per_underlying
        }
        for p in self._valued_positions:
            quote = self.quotes.get_ticker("option", p.symbol)
            if quote and quote.delta is not None:
                base = p.symbol.split("-")[0]
                delta[base] = delta.get(base, ZERO) + quote.delta * p.size * (
                    1 if p.side == "Buy" else -1
                )
        for order in orders:
            quote = self.quotes.get_ticker(order.category, order.symbol)
            per_unit = (
                Decimal("1") if order.category == "spot" else (quote.delta if quote else None)
            )
            if per_unit is None:
                per_unit = Decimal("Infinity")
            change = (
                (order.qty - order.qty_filled)
                * per_unit
                * (1 if order.side.lower() == "buy" else -1)
            )
            exposures.append(
                OpenOrderExposure(
                    order.id,
                    order.symbol,
                    order.side,
                    order.qty - order.qty_filled,
                    change,
                    order.order_link_id,
                )
            )
        paused = [row.reason_code for row in await self.pauses.active_pauses()]
        trusted = (
            self.identity_ok
            and self.reconcile_ok
            and self.account_at is not None
            and (now_utc() - self.account_at).total_seconds()
            <= self.config.market.account_state_max_age_seconds
        )
        return RiskContext(
            account=self.account,
            positions=self._valued_positions,
            quotes={symbol: quote for (_, symbol), quote in self.quotes._quotes.items()},
            open_orders=exposures,
            strategy_spot_exposure=strategy_spot,
            account_spot_exposure=account_spot,
            account_premium_used=premium,
            premium_used_per_underlying=by_base,
            premium_used_per_expiry=by_expiry,
            open_order_count=len(orders),
            daily_pnl=max(ZERO, -self.daily_pnl),
            drawdown=self.drawdown,
            account_pause_reasons=paused,
            unknown_order_count=sum(order.status in ("UNKNOWN", "SUBMITTING") for order in orders),
            execution_authority=self.lease.state is (LeaseState.ELIGIBLE),
            market_trusted=self.quotes.status(
                intent.category, intent.symbol, require_greeks=intent.category == "option"
            ).usable,
            account_trusted=trusted,
            config_version=self._config_hash,
            valuation_trusted=self.baseline_ok and self.valuation.complete,
            identity_trusted=self.identity_ok,
            account_delta_per_underlying=delta,
            instruments={spec.symbol: spec for spec in self.specs.values()},
        )

    async def _cancel_open(self):
        command = ControlCommand(kind=CommandKind.CANCEL_BOT_OPEN_ORDERS, payload={})
        for order in await self.commands._resolve_cancel_targets(command):
            await self.executor.cancel(order)

    async def _cancel_expired(self, *, paused):
        async with self.database.sessionmaker() as session:
            pairs = (
                await session.execute(
                    select(Order, TradeIntent, Strategy)
                    .join(TradeIntent, TradeIntent.id == Order.intent_id)
                    .outerjoin(Strategy, Strategy.id == TradeIntent.strategy_id)
                    .where(
                        Order.environment == self.environment, Order.status.notin_(TERMINAL_STATES)
                    )
                )
            ).all()
        hour = now_utc().hour
        start, end = (
            self.config.option.no_open_window_start_hour_utc,
            self.config.option.no_open_window_end_hour_utc,
        )
        window = start != end and (
            (start <= hour < end) if start < end else (hour >= start or hour < end)
        )
        for order, intent, strategy in pairs:
            opening = intent.action in OPEN_ACTIONS
            expired = order.local_ttl_expires_at and order.local_ttl_expires_at <= now_utc()
            if (
                expired
                or opening
                and (
                    paused
                    or not strategy
                    or not strategy.enabled
                    or (order.category == "option" and window)
                )
            ):
                resolution = await self.executor.cancel(order)
                if not resolution.resolved:
                    await self._fault(
                        "CANCEL_PENDING", f"订单 {order.order_link_id} 尚未确认终结", locked=False
                    )

    async def _exit_rules(self):
        if not self.identity_ok or not self.reconcile_ok:
            return
        async with self.database.sessionmaker() as session:
            inventory = list(
                await session.scalars(
                    select(Inventory).where(
                        Inventory.environment == self.environment,
                        Inventory.qty > 0,
                        Inventory.trusted.is_(True),
                    )
                )
            )
        for item in inventory:
            if item.strategy_id not in self.strategies:
                continue
            # 库存归属到具体持有周期（CR-01）：完整平仓后的新买入开启新周期，
            # 周期内追加买入保留原起点；已平掉的历史批次不再驱动新的计时。
            owner = None
            episode_start = item.episode_started_at
            if item.episode_intent_id is not None:
                async with self.database.sessionmaker() as session:
                    owner = await session.scalar(
                        select(TradeIntent).where(
                            TradeIntent.environment == self.environment,
                            TradeIntent.id == item.episode_intent_id,
                            TradeIntent.strategy_id == item.strategy_id,
                            TradeIntent.symbol == item.symbol,
                        )
                    )
            if owner is None or episode_start is None:
                await self._fault(
                    "EXIT_INCOMPLETE",
                    f"{item.symbol} 库存持有周期缺少成交归属，待核实",
                    locked=False,
                )
                continue
            if not owner.exit_rules:
                continue
            category = "option" if "-" in item.symbol else "spot"
            quote = self.quotes.get_ticker(category, item.symbol)
            spec = self.specs.get((category, item.symbol))
            if spec is None or not spec.qty_step or not spec.tick_size:
                continue
            if spec.delivery_time and spec.delivery_time <= now_utc():
                await self._fault(
                    "SETTLEMENT_PENDING", f"{item.symbol} 到期但结算尚未核对，保留成本预算"
                )
                continue
            async with self.database.sessionmaker() as session:
                previous = await session.scalar(
                    select(TradeIntent)
                    .where(
                        TradeIntent.environment == self.environment,
                        TradeIntent.strategy_id == item.strategy_id,
                        TradeIntent.symbol == item.symbol,
                        TradeIntent.signal_id.like(f"exit:{owner.id}:%"),
                    )
                    .order_by(TradeIntent.id.desc())
                    .limit(1)
                )
            # 一个持有周期只有一个退出流程；后续追加买入不重置追价次数和期限。
            if previous is not None:
                await self._continue_exit(previous, item, spec)
                continue
            trigger = None
            for rule in owner.exit_rules:
                kind = rule.get("kind")
                due = (
                    kind == "hold_period"
                    and (now_utc() - episode_start).total_seconds() >= rule["hold_seconds"]
                ) or (
                    kind == "before_expiry"
                    and spec.delivery_time
                    and (spec.delivery_time - now_utc()).total_seconds()
                    <= self.config.option.exit_before_expiry_seconds
                )
                if (
                    kind == "profit_threshold"
                    and quote
                    and quote.bid_price
                    and item.avg_cost > 0
                ):
                    rate = self._fee_ratio(category, item.symbol, quote.bid_price)
                    proceeds = quote.bid_price * (1 - rate)
                    due = (proceeds / item.avg_cost - 1) * 100 >= Decimal(
                        rule["profit_threshold_pct"]
                    )
                if due and trigger is None:
                    trigger = (owner, rule)
            if trigger is None:
                continue
            if (
                not self.quotes.status(category, item.symbol).usable
                or not quote
                or not quote.bid_price
            ):
                await self._fault(
                    "EXIT_INCOMPLETE", f"{item.symbol} 没有有效退出报价，仍有持仓", locked=False
                )
                continue
            owner, rule = trigger
            async with self.database.sessionmaker() as session:
                pending = await self.reservations._pending_close_qty(
                    session, item.symbol, item.strategy_id
                )
            qty = round_to_step(max(ZERO, item.qty - pending), spec.qty_step, Rounding.DOWN)
            if qty <= 0:
                continue
            # 底线同时受执行配置和独立风控价格偏离约束。
            boundary_bps = min(
                self.config.execution.exit_price_floor_bps,
                self.config.market.max_price_deviation_bps,
            )
            floor = round_to_step(
                quote.bid_price * (1 - boundary_bps / 10000), spec.tick_size, Rounding.UP
            )
            price = round_to_step(quote.bid_price, spec.tick_size, Rounding.UP)
            if floor <= 0:
                continue
            validity = int(rule.get("exit_validity_seconds", 300))
            max_rebuilds = min(
                int(rule.get("max_reprice_attempts", 0)), self.config.execution.max_rebuild_attempts
            )
            segment = max(1, validity // (max_rebuilds + 1))
            draft = IntentDraft(
                action="OPTION_SELL_CLOSE" if category == "option" else "SPOT_SELL",
                category=category,
                symbol=item.symbol,
                side="Sell",
                qty=qty,
                qty_unit=owner.qty_unit,
                currency=item.currency,
                spend_limit=ZERO,
                price_limit=floor,
                reduce_qty=qty,
                underlying=owner.underlying,
                ttl_seconds=validity,
                exit_rules=({**rule, "max_reprice_attempts": max_rebuilds},),
                rationale={
                    "inventory_scope": "strategy_symbol",
                    "owner_intent_id": owner.id,
                    "inventory_episode_start": episode_start.isoformat(),
                    "price_floor": str(floor),
                    "initial_bid": str(quote.bid_price),
                },
            )
            result = await self._submit_draft(
                item.strategy_id,
                owner.strategy_version,
                draft,
                planned_at=now_utc(),
                rule_id="exit",
                signal=f"exit:{owner.id}:{rule['kind']}:{episode_start.isoformat()}",
                order_price=price,
                order_ttl_seconds=segment,
            )
            if result["status"] == "REJECTED":
                await self._fault(
                    "EXIT_INCOMPLETE", f"{item.symbol} 退出被拒绝，仍有持仓", locked=False
                )

    async def _continue_exit(self, intent, item, spec):
        if intent.status != "APPROVED":
            return
        async with self.database.sessionmaker() as session:
            parent = await session.scalar(
                select(Order)
                .where(Order.intent_id == intent.id)
                .order_by(Order.rebuild_index.desc())
                .limit(1)
            )
        if (
            parent is None
            or parent.status not in TERMINAL_STATES
            or parent.sync_state != "CONFIRMED"
        ):
            return
        filled, pending, _ = await self.executor.rebuild_bounds(intent.id)
        remaining = round_to_step(
            min(item.qty, max(ZERO, intent.qty - filled)), spec.qty_step, Rounding.DOWN
        )
        if remaining <= 0 or pending:
            return
        rule = intent.exit_rules[0] if intent.exit_rules else {}
        maximum = min(
            int(rule.get("max_reprice_attempts", 0)), self.config.execution.max_rebuild_attempts
        )
        if now_utc() >= intent.expires_at or parent.rebuild_index >= maximum:
            await self._fault(
                "EXIT_INCOMPLETE",
                f"{item.symbol} 退出期限/追价次数已用尽，剩余 {remaining}",
                locked=False,
            )
            return
        await self._account_sync()
        quote = await self._quote(intent.category, intent.symbol, depth=True)
        if not self.quotes.status(intent.category, intent.symbol).usable or not quote.bid_price:
            return
        price = round_to_step(quote.bid_price, spec.tick_size, Rounding.UP)
        if price < intent.price_limit:
            await self._fault(
                "EXIT_INCOMPLETE",
                f"{item.symbol} 买价低于原退出底线，剩余 {remaining}",
                locked=False,
            )
            return
        async with self.database.sessionmaker() as session:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                {"key": f"{self.environment}:account-authorization"},
            )
            evaluation = await RiskEngine(self.config).evaluate(
                intent, await self._risk_context(session, intent)
            )
            qty = round_to_step(remaining, spec.qty_step, Rounding.DOWN)
            if not evaluation.approved or not self.reconcile_ok or not self.identity_ok:
                return
            if spec.min_order_qty and qty < spec.min_order_qty:
                return
            if spec.min_order_amt and qty * price < spec.min_order_amt:
                return
            if intent.category == "spot":
                balance = self.account.coin(spec.base_coin)
                account_qty, account_available = balance.wallet_balance, balance.available
            else:
                account_qty = sum(
                    (
                        p.size
                        for p in self.positions
                        if p.symbol == intent.symbol and p.side == "Buy"
                    ),
                    ZERO,
                )
                account_available = None
            if account_qty is None:
                return
            reservation = await self.reservations.reserve_inventory(
                session,
                intent_id=intent.id,
                symbol=intent.symbol,
                qty=qty,
                account_qty=account_qty,
                account_available=account_available,
                account_trusted=True,
                strategy_id=intent.strategy_id,
            )
            await self.ledger.append_event(
                session,
                event_type="EXIT_REBUILD_AUTHORIZED",
                dedupe_key=f"exit-rebuild:{parent.id}",
                occurred_at=now_utc(),
                payload={
                    "category": intent.category,
                    "intent_id": intent.id,
                    "parent_order_id": parent.id,
                    "qty": str(qty),
                    "price": str(price),
                    "checks": [check.as_dict() for check in evaluation.checks],
                },
            )
            await session.commit()
        validity = int(rule.get("exit_validity_seconds", 300))
        ttl = min(
            intent.expires_at, now_utc() + dt.timedelta(seconds=max(1, validity // (maximum + 1)))
        )
        result = await self.executor.rebuild(
            parent=parent,
            max_rebuilds=maximum,
            plan=OrderPlan(
                intent_id=intent.id,
                category=intent.category,
                symbol=intent.symbol,
                side=intent.side,
                qty=qty,
                price=price,
                reduce_only=intent.category == "option",
                position_idx=0 if intent.category == "option" else None,
                strategy_id=intent.strategy_id,
                reservation_id=reservation.id,
                local_ttl_expires_at=ttl,
            ),
        )
        if result.is_unknown:
            await self._fault(PauseReason.UNKNOWN_ORDER, "退出重建结果未知，保留预留")

    async def _notifications(self):
        await deliver_alerts(self.alerts, self.config.ops.alert_channels)
        pending = await self.alerts.undelivered()
        for alert in pending:
            episode = (alert.context or {}).get("episode_started_at")
            started = dt.datetime.fromisoformat(episode) if episode else alert.first_seen_at
            if alert.severity == "CRITICAL" and (now_utc() - started).total_seconds() >= (
                self.config.ops.notification_failure_pause_seconds
            ):
                await self.pauses.pause(
                    reason_code=PauseReason.NOTIFICATION_FAILURE,
                    reason_detail="关键告警尚未送达外部渠道",
                    locked=False,
                )

    async def _status(self):
        await self.store.put(
            "status",
            {
                "state": self.machine.state.value,
                "lease": self.lease.state.value,
                "account_uid": self.uid,
                "environment": self.environment,
                "account_captured_at": self.account_at,
                "tick_at": now_utc(),
                "reconciled_at": self.reconciled_at,
                "last_error": self.last_error,
                "equity": str(self.valuation.equity)
                if self.valuation and self.valuation.complete
                else None,
                "currency": self.config.ops.valuation.currency,
                "daily_pnl": str(self.daily_pnl) if self.baseline_ok else None,
                "drawdown": str(self.drawdown) if self.baseline_ok else None,
                "valuation_complete": bool(self.valuation and self.valuation.complete),
                "baseline_available": self.baseline_ok,
                "config_version": self._config_hash,
                "source": "paper 模拟，保守可见深度模型"
                if self.environment == "paper"
                else self.environment,
                "clock_checked": self._clock_at > 0,
                "capabilities": {
                    "option_units": self.environment == "paper"
                    or (self.config.runtime.option_units_verified),
                    "option_reduce_only": self.environment == "paper"
                    or (self.config.runtime.option_reduce_only_verified),
                },
            },
        )
