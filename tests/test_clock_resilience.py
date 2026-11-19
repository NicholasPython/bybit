"""时钟跳变、交易所偏差与调度幂等（AC-26 / NFR-06 / STR-02）。

需求原文：
    **AC-26** 模拟系统时钟前跳、回拨和超出允许偏差；暂停新开仓，调度不重复补做，
    超时/退避时长不被墙上时钟跳变破坏，时间恢复仍须满足对账和暂停解除条件。

本文件分三层证据：

    * :class:`ClockWatch` 的跳变与偏差判定（纯单元，可确定性复现）；
    * :class:`Deadline` 与退避不受墙上时钟影响（纯单元）；
    * 服务层面的暂停、恢复条件与调度幂等（真实链路 + 数据库）。
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import uuid
from decimal import Decimal as D
from pathlib import Path

import pytest
from sqlalchemy import func, select

from bybitbot import clock as clock_module
from bybitbot.adapters.base import AccountView, CoinBalance, InstrumentSpec, OrderBook, Ticker
from bybitbot.adapters.paper import PaperAdapter
from bybitbot.clock import Deadline, backoff_delay, now_utc
from bybitbot.db.models import RiskPause, ScheduleRun, Strategy, TradeIntent
from bybitbot.execution.lease import LeaseState
from bybitbot.ops.clockwatch import ClockWatch
from bybitbot.ops.control import CommandKind
from bybitbot.ops.pauses import PauseReason
from bybitbot.ops.state import RunState
from bybitbot.scheduler import Scheduler
from bybitbot.service import TradingService
from bybitbot.settings.loader import load_config
from bybitbot.strategy.base import IntentDraft
from bybitbot.valuation import Valuation

MAX_SKEW_SECONDS = 5.0


# --------------------------------------------------------------------------
# 测试替身
# --------------------------------------------------------------------------


class FakeClock:
    """可控的墙钟与单调钟。

    墙上时钟可以任意跳变，单调钟单独推进 —— 这样"时钟跳变"与"时间流逝"
    才能被分开观察。
    """

    def __init__(self, wall: dt.datetime | None = None, monotonic: float = 1000.0):
        self.wall = wall or now_utc()
        self.monotonic = monotonic

    def wall_now(self) -> dt.datetime:
        return self.wall

    def mono_now(self) -> float:
        return self.monotonic

    def advance(self, *, wall_seconds: float = 0.0, monotonic_seconds: float = 0.0):
        self.wall = self.wall + dt.timedelta(seconds=wall_seconds)
        self.monotonic += monotonic_seconds


def attach_clock(service, *, skew_interval: float = 60.0, recovery_interval: float = 10.0):
    """给服务装上可控时钟，返回 ``(FakeClock, ClockWatch)``。"""
    fake = FakeClock()
    watch = ClockWatch(
        max_skew_seconds=MAX_SKEW_SECONDS,
        skew_interval_seconds=skew_interval,
        recovery_interval_seconds=recovery_interval,
        wall_clock=fake.wall_now,
        monotonic_clock=fake.mono_now,
    )
    service.clock_watch = watch
    return fake, watch


class SkewMarket(PaperAdapter):
    """可控制交易所时间偏差的模拟行情源。"""

    def __init__(self, *, skew_seconds: float = 0.0):
        super().__init__()
        self.skew_seconds = skew_seconds
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

    async def get_server_time(self) -> dt.datetime:
        return now_utc() + dt.timedelta(seconds=self.skew_seconds)

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
    async with service.database.sessionmaker() as session:
        return await session.get(type(row), row.id)


def enabled_config():
    """打开现货策略的示例配置 —— 没有启用的策略就没有调度可观察。"""
    config = load_config("config/config.example.yaml")
    strategies = tuple(
        s.model_copy(update={"enabled": True}) if s.kind == "spot_fixed_budget_dca" else s
        for s in config.strategies
    )
    return config.model_copy(update={"strategies": strategies})


@pytest.fixture
async def running_service(database):
    """已经接管、已建立基线、可以正常跑的服务。

    测试结束必须关闭服务 —— 执行资格的互斥锁是数据库级咨询锁，
    不释放会挡住后续测试。
    """
    service = TradingService(enabled_config(), database, market_adapter=SkewMarket())
    await service.start()
    await command(service, CommandKind.INITIALIZE_BASELINE, {"reason": "时钟证据测试"})
    await command(
        service,
        CommandKind.CONFIRM_TAKEOVER,
        {"reason": "时钟证据测试", "evidence": "隔离 schema 内的唯一 worker"},
    )
    await service.tick()
    try:
        yield service
    finally:
        await service.close()


async def count_intents(database) -> int:
    async with database.sessionmaker() as session:
        return int(
            await session.scalar(select(func.count()).select_from(TradeIntent)) or 0
        )


def account_view(*, debt: bool):
    """构造一个账户视图；``debt=True`` 表示存在未预期负债。"""
    borrowed = D("5") if debt else D("0")
    return AccountView(
        account_uid="paper-account",
        account_mode="unified",
        coins={
            "USDT": CoinBalance(
                coin="USDT",
                equity=D("1000"),
                available=D("1000"),
                wallet_balance=D("1000"),
                borrowed=borrowed,
            )
        },
    )


# --------------------------------------------------------------------------
# 跳变与偏差判定
# --------------------------------------------------------------------------


def test_forward_and_backward_jumps_are_both_detected():
    fake = FakeClock()
    watch = ClockWatch(max_skew_seconds=MAX_SKEW_SECONDS, wall_clock=fake.wall_now,
                        monotonic_clock=fake.mono_now)
    fake.advance(wall_seconds=3600, monotonic_seconds=1)
    forward = watch.observe()
    assert forward.jumped is True
    assert forward.direction == "forward"
    assert forward.jump_seconds == pytest.approx(3599.0)

    fake.advance(wall_seconds=-1800, monotonic_seconds=1)
    backward = watch.observe()
    assert backward.jumped is True
    assert backward.direction == "backward"
    assert backward.jump_seconds == pytest.approx(1801.0)


def test_wall_and_monotonic_together_do_not_look_like_a_jump():
    fake = FakeClock()
    watch = ClockWatch(max_skew_seconds=MAX_SKEW_SECONDS, wall_clock=fake.wall_now,
                        monotonic_clock=fake.mono_now)
    fake.advance(wall_seconds=30, monotonic_seconds=30)
    observation = watch.observe()
    assert observation.jumped is False
    assert observation.direction == "none"
    assert observation.jump_seconds == pytest.approx(0.0)


def test_jump_is_reported_once_and_does_not_repeat_every_tick():
    fake = FakeClock()
    watch = ClockWatch(max_skew_seconds=MAX_SKEW_SECONDS, wall_clock=fake.wall_now,
                        monotonic_clock=fake.mono_now)
    fake.advance(wall_seconds=7200, monotonic_seconds=1)
    assert watch.observe().jumped is True
    # 之后的 tick 不再重复报跳变：暂停由暂停表保持，不靠重复上报维持。
    assert watch.observe().jumped is False
    assert watch.last_jump is not None
    assert watch.last_jump.direction == "forward"


def test_skew_measurement_uses_the_round_trip_midpoint():
    fake = FakeClock()
    watch = ClockWatch(max_skew_seconds=MAX_SKEW_SECONDS, wall_clock=fake.wall_now,
                        monotonic_clock=fake.mono_now)
    before = dt.datetime(2026, 11, 17, 12, 0, 0, tzinfo=dt.UTC)
    after = before + dt.timedelta(milliseconds=200)
    remote = before + dt.timedelta(seconds=30)
    observation = watch.measure_skew(local_before=before, local_after=after, remote=remote)
    # 交易所时间领先本机 30 秒，往返 0.2 秒被中点抵消。
    assert observation.offset_seconds == pytest.approx(30.0, abs=0.2)
    assert observation.exceeded is True
    assert observation.round_trip_seconds == pytest.approx(0.2)


def test_skew_within_threshold_is_acceptable():
    fake = FakeClock()
    watch = ClockWatch(max_skew_seconds=MAX_SKEW_SECONDS, wall_clock=fake.wall_now,
                        monotonic_clock=fake.mono_now)
    before = dt.datetime(2026, 11, 17, 12, 0, 0, tzinfo=dt.UTC)
    observation = watch.measure_skew(
        local_before=before, local_after=before, remote=before + dt.timedelta(seconds=1)
    )
    assert observation.exceeded is False


def test_skew_measurement_rejects_naive_exchange_time():
    fake = FakeClock()
    watch = ClockWatch(max_skew_seconds=MAX_SKEW_SECONDS, wall_clock=fake.wall_now,
                        monotonic_clock=fake.mono_now)
    moment = dt.datetime(2026, 11, 17, 12, 0, 0)
    with pytest.raises(ValueError, match="时区"):
        watch.measure_skew(local_before=moment, local_after=moment, remote=moment)


def test_skew_schedule_respects_interval_and_recovery_interval():
    fake = FakeClock()
    watch = ClockWatch(
        max_skew_seconds=MAX_SKEW_SECONDS,
        skew_interval_seconds=60.0,
        recovery_interval_seconds=10.0,
        wall_clock=fake.wall_now,
        monotonic_clock=fake.mono_now,
    )
    assert watch.skew_due() is True
    watch.mark_skew_checked()
    fake.advance(monotonic_seconds=5)
    assert watch.skew_due() is False
    assert watch.skew_due(recovery_pending=True) is False
    fake.advance(monotonic_seconds=30)
    assert watch.skew_due() is False
    # 有未解除的偏差暂停时按更短的复测间隔确认恢复。
    assert watch.skew_due(recovery_pending=True) is True
    fake.advance(monotonic_seconds=30)
    assert watch.skew_due() is True
    watch.mark_skew_checked()
    fake.advance(monotonic_seconds=11)
    assert watch.skew_due() is False
    assert watch.skew_due(recovery_pending=True) is True


# --------------------------------------------------------------------------
# 超时与退避不受墙上时钟影响
# --------------------------------------------------------------------------


def test_deadline_is_unaffected_by_wall_clock_jumps(monkeypatch):
    mono = {"now": 100.0}
    wall = {"now": dt.datetime(2026, 11, 17, 12, 0, 0, tzinfo=dt.UTC)}
    monkeypatch.setattr(clock_module, "monotonic", lambda: mono["now"])
    monkeypatch.setattr(clock_module, "now_utc", lambda: wall["now"])

    deadline = Deadline(60)
    wall["now"] += dt.timedelta(hours=1)
    assert deadline.expired is False
    assert deadline.remaining == pytest.approx(60.0)

    wall["now"] -= dt.timedelta(hours=2)
    assert deadline.expired is False
    assert deadline.remaining == pytest.approx(60.0)

    mono["now"] += 59.9
    assert deadline.expired is False
    mono["now"] += 0.2
    assert deadline.expired is True
    assert deadline.remaining == 0.0


def test_backoff_is_deterministic_and_capped():
    assert backoff_delay(3) == backoff_delay(3)
    assert backoff_delay(3) != backoff_delay(4)
    assert 0.4 <= backoff_delay(0) <= 0.6
    assert backoff_delay(30) <= 30.0 * 1.2
    with pytest.raises(ValueError):
        backoff_delay(-1)


# --------------------------------------------------------------------------
# 服务层面：跳变暂停开仓
# --------------------------------------------------------------------------


async def test_clock_jump_pauses_new_exposure_and_stays_locked(database, running_service):
    service = running_service
    fake, _ = attach_clock(service)
    assert service.machine.state is RunState.RUNNING

    before = await count_intents(database)
    fake.advance(wall_seconds=3600, monotonic_seconds=5)
    await service.tick()

    assert service.machine.state is RunState.PAUSED_OPEN
    assert service.machine.allows_new_exposure is False

    async with database.sessionmaker() as session:
        pause = await session.scalar(
            select(RiskPause).where(
                RiskPause.environment == "paper",
                RiskPause.reason_code == PauseReason.CLOCK_JUMP,
                RiskPause.active.is_(True),
            )
        )
    assert pause is not None
    assert pause.locked is True
    assert "前跳" in pause.reason_detail
    pause_evidence = {
        "jump_seconds": service.clock_watch.last_jump.jump_seconds,
        "direction": service.clock_watch.last_jump.direction,
    }

    keys = {view.alert_key for view in await service.alerts.active()}
    assert PauseReason.CLOCK_JUMP in keys

    # 暂停期间不接受新开仓意图。
    strategy_id, (_, version) = next(iter(service.strategies.items()))
    result = await service._submit_draft(
        strategy_id,
        version,
        IntentDraft(
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
        ),
        planned_at=now_utc(),
    )
    assert result["status"] == "REJECTED"
    assert "暂停新增风险" in result["reason"]
    # 被拒绝的意图仍然留痕（审计要求），但没有任何被批准的新意图。
    async with database.sessionmaker() as session:
        rows = list(await session.scalars(select(TradeIntent)))
    assert await count_intents(database) == before + 1
    assert {row.status for row in rows} == {"REJECTED"}

    evidence = {
        "generated_at": now_utc().isoformat(),
        "environment": "paper",
        "scenario": "系统时钟前跳 3600 秒",
        "jump_seconds": pause_evidence["jump_seconds"],
        "jump_direction": pause_evidence["direction"],
        "max_skew_seconds": MAX_SKEW_SECONDS,
        "pause_reason": PauseReason.CLOCK_JUMP,
        "pause_locked": pause.locked,
        "machine_state": service.machine.state.value,
        "allows_new_exposure": service.machine.allows_new_exposure,
        "rejected_reason": result["reason"],
        "intents_before": before,
        "intents_after": await count_intents(database),
        "approved_intents": 0,
    }
    path = os.environ.get("BYBITBOT_EVIDENCE_PATH")
    if path:
        await asyncio.to_thread(
            Path(path).write_text,
            json.dumps(evidence, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


async def test_clock_jump_pause_survives_restart_and_technical_resync(
    database, running_service
):
    service = running_service
    fake, _ = attach_clock(service)
    fake.advance(wall_seconds=-1800, monotonic_seconds=5)
    await service.tick()

    async with database.sessionmaker() as session:
        pause = await session.scalar(
            select(RiskPause).where(
                RiskPause.environment == "paper",
                RiskPause.reason_code == PauseReason.CLOCK_JUMP,
                RiskPause.active.is_(True),
            )
        )
    assert pause is not None
    assert pause.locked is True

    # 技术性重新同步不能解除锁定暂停。
    await service._clear_technical(PauseReason.CLOCK_JUMP)
    async with database.sessionmaker() as session:
        still = await session.get(RiskPause, pause.id)
    assert still.active is True

    # 重启也不能解除暂停（暂停是持久化的）。
    await service.close()
    restarted = TradingService(enabled_config(), database, market_adapter=SkewMarket())
    reasons = {row.reason_code for row in await restarted.pauses.active_pauses()}
    assert PauseReason.CLOCK_JUMP in reasons


async def test_clock_jump_requires_manual_resume_with_reconciliation(database, running_service):
    service = running_service
    fake, _ = attach_clock(service)
    fake.advance(wall_seconds=3600, monotonic_seconds=5)
    await service.tick()

    async with database.sessionmaker() as session:
        pause = await session.scalar(
            select(RiskPause).where(
                RiskPause.environment == "paper",
                RiskPause.reason_code == PauseReason.CLOCK_JUMP,
                RiskPause.active.is_(True),
            )
        )
    row = await command(
        service,
        CommandKind.RESUME,
        {"pause_id": pause.id, "reason": "人工确认时钟已校准"},
    )
    assert row.status == "EXECUTED"
    async with database.sessionmaker() as session:
        resolved = await session.get(RiskPause, pause.id)
    assert resolved.active is False
    assert resolved.resolved_by == "test-operator"


async def test_clock_skew_does_not_widen_the_request_window(database, running_service):
    """NFR-06：不通过无限增大请求窗口掩盖偏差。"""
    service = running_service
    service.market_adapter.skew_seconds = 60
    attach_clock(service)
    await service.tick()

    # 请求超时与挂单有效期仍然由配置决定，偏差不会把它们改大。
    assert service.config.execution.request_timeout_seconds == 30
    assert service.config.execution.order_ttl_seconds == 3600

    strategy_id, (_, version) = next(iter(service.strategies.items()))
    planned_at = now_utc()
    await service._submit_draft(
        strategy_id,
        version,
        IntentDraft(
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
        ),
        planned_at=planned_at,
    )
    async with database.sessionmaker() as session:
        intent = await session.scalar(select(TradeIntent))
    assert intent is not None
    # 有效期仍然是"计划时刻 + 配置 TTL"，没有被偏差或墙钟拉长。
    assert (intent.expires_at - planned_at).total_seconds() == pytest.approx(60.0)


# --------------------------------------------------------------------------
# 服务层面：交易所偏差
# --------------------------------------------------------------------------


async def test_exchange_skew_beyond_threshold_pauses_new_exposure(database, running_service):
    service = running_service
    service.market_adapter.skew_seconds = 60
    attach_clock(service)
    await service.tick()

    assert service.machine.state is RunState.PAUSED_OPEN
    async with database.sessionmaker() as session:
        pause = await session.scalar(
            select(RiskPause).where(
                RiskPause.environment == "paper",
                RiskPause.reason_code == PauseReason.CLOCK_SKEW,
                RiskPause.active.is_(True),
            )
        )
    assert pause is not None
    assert pause.locked is False
    assert "偏差" in pause.reason_detail


async def test_skew_recovery_waits_for_reconciliation_conditions(database, running_service):
    service = running_service
    market = service.market_adapter
    market.skew_seconds = 60
    fake, _ = attach_clock(service)
    await service.tick()
    assert service.machine.state is RunState.PAUSED_OPEN

    # 时钟恢复正常，但账户对账还没通过 —— 不解除暂停。
    market.skew_seconds = 0
    original = service._account_sync

    async def unreconciled_sync():
        await original()
        service.reconcile_ok = False

    service._account_sync = unreconciled_sync
    # 模拟上一轮对账仍有差异：解除条件不成立。
    service.reconcile_ok = False
    fake.advance(monotonic_seconds=30)
    await service.tick()
    async with database.sessionmaker() as session:
        pause = await session.scalar(
            select(RiskPause).where(
                RiskPause.environment == "paper",
                RiskPause.reason_code == PauseReason.CLOCK_SKEW,
                RiskPause.active.is_(True),
            )
        )
    assert pause is not None, "对账未通过时不得解除时钟暂停"
    assert service.machine.state is RunState.PAUSED_OPEN

    # 对账恢复后，下一次测量确认偏差已回到阈值内 —— 暂停才解除。
    service._account_sync = original
    service.reconcile_ok = True
    fake.advance(monotonic_seconds=30)
    await service.tick()
    async with database.sessionmaker() as session:
        remaining = await session.scalar(
            select(func.count())
            .select_from(RiskPause)
            .where(
                RiskPause.environment == "paper",
                RiskPause.reason_code == PauseReason.CLOCK_SKEW,
                RiskPause.active.is_(True),
            )
        )
    assert remaining == 0


async def test_clock_recovery_requires_every_release_condition(database):
    service = TradingService(enabled_config(), database, market_adapter=SkewMarket())
    service.identity_ok = True
    service.reconcile_ok = True
    service.baseline_ok = True
    # 直接改租约状态：这里只关心解除条件，不关心接管流程。
    service.lease._state = LeaseState.ELIGIBLE
    service.valuation = Valuation(
        equity=D("1000"), currency="USDT", policy_version="equity-v1", complete=True
    )
    service.account = account_view(debt=False)
    assert await service._clock_recovery_ready() is True

    service.reconcile_ok = False
    assert await service._clock_recovery_ready() is False
    service.reconcile_ok = True

    service.baseline_ok = False
    assert await service._clock_recovery_ready() is False
    service.baseline_ok = True

    service.lease._state = LeaseState.NEEDS_CONFIRMATION
    assert await service._clock_recovery_ready() is False
    # 直接改租约状态：这里只关心解除条件，不关心接管流程。
    service.lease._state = LeaseState.ELIGIBLE

    service.account = account_view(debt=True)
    assert await service._clock_recovery_ready() is False


async def make_strategy(sessionmaker, name: str = "sched") -> int:
    """调度行有指向策略实例的外键，先建一条策略。"""
    async with sessionmaker() as session:
        strategy = Strategy(
            environment="paper",
            name=name,
            kind="spot_fixed_budget_dca",
            enabled=True,
            created_at=now_utc(),
        )
        session.add(strategy)
        await session.commit()
        return strategy.id


# --------------------------------------------------------------------------
# 调度幂等：不重复、不补做
# --------------------------------------------------------------------------


async def test_missed_slot_is_skipped_instead_of_bought_back(sessionmaker):
    scheduler = Scheduler(sessionmaker, "paper", skip_grace_seconds=60)
    strategy_id = await make_strategy(sessionmaker)
    planned_at = now_utc() - dt.timedelta(hours=1)
    run = await scheduler.plan(
        strategy_id=strategy_id, strategy_version=1, rule_id="BTCUSDT:00:00",
        planned_at=planned_at,
    )
    assert run is not None
    assert await scheduler.claim_due() == []
    async with sessionmaker() as session:
        row = await session.get(ScheduleRun, run.id)
    assert row.status == "SKIPPED"
    assert row.result["reason"] == "missed"


async def test_same_utc_slot_is_planned_once_even_after_backward_jump(sessionmaker):
    scheduler = Scheduler(sessionmaker, "paper")
    strategy_id = await make_strategy(sessionmaker)
    planned_at = now_utc().replace(second=0, microsecond=0)
    first = await scheduler.plan(
        strategy_id=strategy_id, strategy_version=1, rule_id="BTCUSDT:00:00",
        planned_at=planned_at,
    )
    assert first is not None
    # 时钟回拨后同一 UTC 计划时刻仍然是同一个去重键。
    second = await scheduler.plan(
        strategy_id=strategy_id, strategy_version=1, rule_id="BTCUSDT:00:00",
        planned_at=planned_at,
    )
    assert second is None
    claimed = await scheduler.claim_due()
    assert len(claimed) == 1


async def test_clock_jump_does_not_trigger_catch_up_buys(database, running_service):
    service = running_service
    fake, _ = attach_clock(service)
    before = await count_intents(database)

    # 时钟前跳一小时：错过的计划时刻被跳过，不集中补买。
    fake.advance(wall_seconds=3600, monotonic_seconds=5)
    await service.tick()
    assert await count_intents(database) == before

    # 时钟回拨跨过 UTC 日界线：昨天的时刻同样被跳过。
    fake.advance(wall_seconds=-7200, monotonic_seconds=5)
    await service.tick()
    assert await count_intents(database) == before
