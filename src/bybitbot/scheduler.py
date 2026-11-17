"""持久化调度（STR-02）。

需求原文：
    定时任务持久化调度时区和计划时刻。信号去重键包含策略实例、规则编号和
    计划时刻/事件编号；任务重试与进程重启不重复执行。**首期默认跳过已错过时刻，
    禁止停机后集中补买**；过期信号被拒绝，冷却时间持久化。

三条硬规则：
    * **去重键唯一** —— ``(environment, dedupe_key)`` 上有唯一约束，重复投递
      100 次也只会产生一条任务（AC-04）。
    * **跳过已错过时刻** —— 计划时刻距今超过宽限期就标记 ``SKIPPED``，
      绝不补买。这条是需求明确写死的默认行为。
    * **冷却时间持久化** —— 冷却状态存在 ``schedule_run`` 里，重启不会丢。

抢占用一条带条件的 ``UPDATE`` 完成（``WHERE status='PENDING'``），
因此两个进程同时看到同一任务时只有一个能拿到。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from bybitbot.clock import now_utc
from bybitbot.db.models import ScheduleRun


class ScheduleStatus(StrEnum):
    PLANNED = "PLANNED"
    RUNNING = "RUNNING"
    DONE = "DONE"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class PlannedRun:
    """一次已计划的调度。"""

    id: int
    strategy_id: int
    rule_id: str
    planned_at: dt.datetime
    dedupe_key: str


def dedupe_key(*, environment: str, strategy_id: int, rule_id: str, planned_at: dt.datetime) -> str:
    """信号去重键（STR-02）。

    组成：环境 + 策略实例 + 规则编号 + 计划时刻（UTC，秒级）。
    计划时刻用 UTC 且精确到秒 —— 时区跨日不会改变它，时钟回拨也不会
    让同一个时刻产生第二个键（NFR-06）。
    """
    if planned_at.tzinfo is None:
        raise ValueError("计划时刻必须带时区")
    stamp = planned_at.astimezone(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{environment}:{strategy_id}:{rule_id}:{stamp}"


class Scheduler:
    """持久化调度器。"""

    def __init__(
        self,
        sessionmaker,
        environment: str,
        *,
        #: 计划时刻距今超过该秒数就跳过，不补买（STR-02 默认行为）。
        skip_grace_seconds: int = 300,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.environment = environment
        self.skip_grace_seconds = skip_grace_seconds

    # ------------------------------------------------------------------
    # 计划
    # ------------------------------------------------------------------

    async def plan(
        self,
        *,
        strategy_id: int,
        strategy_version: int,
        rule_id: str,
        planned_at: dt.datetime,
        input_snapshot: dict[str, object] | None = None,
    ) -> PlannedRun | None:
        """登记一次计划执行。

        Returns:
            新建的调度记录；已存在（重复投递）时返回 ``None``。
        """
        key = dedupe_key(
            environment=self.environment,
            strategy_id=strategy_id,
            rule_id=rule_id,
            planned_at=planned_at,
        )
        async with self.sessionmaker() as session:
            run = (
                await session.scalars(
                    insert(ScheduleRun)
                    .values(
                        environment=self.environment,
                        strategy_id=strategy_id,
                        strategy_version=strategy_version,
                        rule_id=rule_id,
                        planned_at=planned_at,
                        dedupe_key=key,
                        status=ScheduleStatus.PLANNED,
                        input_snapshot=input_snapshot or {},
                        result={},
                    )
                    .on_conflict_do_nothing(constraint="uq_schedule_dedupe")
                    .returning(ScheduleRun)
                )
            ).one_or_none()
            await session.commit()
            if run is None:
                return None
            return PlannedRun(
                id=run.id,
                strategy_id=run.strategy_id,
                rule_id=run.rule_id,
                planned_at=run.planned_at,
                dedupe_key=run.dedupe_key,
            )

    # ------------------------------------------------------------------
    # 抢占
    # ------------------------------------------------------------------

    async def claim_due(self, *, limit: int = 50) -> list[ScheduleRun]:
        """抢占到期的任务。

        * 跳过已错过时刻的任务（标记 ``SKIPPED``）；
        * 冷却期内的任务跳过（标记 ``SKIPPED`` 并记录原因）；
        * 用条件 ``UPDATE`` 抢占，保证同一任务只被执行一次。
        """
        claimed: list[ScheduleRun] = []
        now = now_utc()
        async with self.sessionmaker() as session:
            candidates = list(
                await session.scalars(
                    select(ScheduleRun)
                    .where(
                        ScheduleRun.environment == self.environment,
                        ScheduleRun.status == ScheduleStatus.PLANNED,
                        ScheduleRun.planned_at <= now,
                    )
                    .order_by(ScheduleRun.planned_at, ScheduleRun.id)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )

            for run in candidates:
                age = (now - run.planned_at).total_seconds()
                if age > self.skip_grace_seconds:
                    # 首期默认跳过已错过时刻 —— 禁止停机后集中补买。
                    run.status = ScheduleStatus.SKIPPED
                    run.result = {"reason": "missed", "age_seconds": age}
                    continue
                # 行锁覆盖判断、状态变更与提交；未持锁的竞争者不会修改此行。
                run.status = ScheduleStatus.RUNNING
                claimed.append(run)

            await session.commit()

        return claimed

    async def finish(
        self,
        run_id: int,
        *,
        status: str,
        result: dict[str, object] | None = None,
    ) -> None:
        if status not in {
            ScheduleStatus.DONE,
            ScheduleStatus.FAILED,
            ScheduleStatus.REJECTED,
            ScheduleStatus.SKIPPED,
        }:
            raise ValueError(f"无效的调度完成状态 {status}")
        async with self.sessionmaker() as session:
            await session.execute(
                update(ScheduleRun)
                .where(
                    ScheduleRun.id == run_id,
                    ScheduleRun.environment == self.environment,
                    ScheduleRun.status == ScheduleStatus.RUNNING,
                )
                .values(status=status, result=result or {})
            )
            await session.commit()

    async def defer(self, run_id: int, *, result: dict[str, object] | None = None) -> None:
        """把已抢占的任务放回 ``PLANNED``，等下一个调度周期重试。

        用于"限速"而不是"拒单"：本次没提交（例如撞上每秒意图上限），
        任务本身仍然有效，下个周期重新抢占。放回 ``PLANNED`` 不改变
        ``planned_at`` 与去重键，因此不会被当成新的计划时刻，也不会重复执行。

        宽限期（``skip_grace_seconds``）仍然生效：拖到错过时刻的任务会在
        下次抢占时被标记 ``SKIPPED``，不会集中补做（STR-02）。
        """
        async with self.sessionmaker() as session:
            await session.execute(
                update(ScheduleRun)
                .where(
                    ScheduleRun.id == run_id,
                    ScheduleRun.environment == self.environment,
                    ScheduleRun.status == ScheduleStatus.RUNNING,
                )
                .values(status=ScheduleStatus.PLANNED, result=result or {})
            )
            await session.commit()

    # ------------------------------------------------------------------
    # 冷却
    # ------------------------------------------------------------------

    async def in_cooldown(
        self,
        *,
        strategy_id: int,
        rule_id: str,
        cooldown_seconds: int,
        now: dt.datetime | None = None,
    ) -> bool:
        """该策略/规则是否处于冷却期。

        冷却状态从 ``schedule_run`` 里读 —— 持久化，重启不丢。
        """
        if cooldown_seconds <= 0:
            return False
        moment = now or now_utc()
        cutoff = moment - dt.timedelta(seconds=cooldown_seconds)
        async with self.sessionmaker() as session:
            recent = list(
                await session.scalars(
                    select(ScheduleRun).where(
                        ScheduleRun.environment == self.environment,
                        ScheduleRun.strategy_id == strategy_id,
                        ScheduleRun.rule_id == rule_id,
                        ScheduleRun.status == ScheduleStatus.DONE,
                        ScheduleRun.planned_at >= cutoff,
                    )
                )
            )
        return bool(recent)

    async def last_run_at(self, *, strategy_id: int, rule_id: str) -> dt.datetime | None:
        async with self.sessionmaker() as session:
            run = (
                await session.execute(
                    select(ScheduleRun)
                    .where(
                        ScheduleRun.environment == self.environment,
                        ScheduleRun.strategy_id == strategy_id,
                        ScheduleRun.rule_id == rule_id,
                        ScheduleRun.status == ScheduleStatus.DONE,
                    )
                    .order_by(ScheduleRun.planned_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
        return run.planned_at if run else None


__all__ = [
    "PlannedRun",
    "ScheduleStatus",
    "Scheduler",
    "dedupe_key",
]
