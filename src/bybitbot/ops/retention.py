"""数据保留与清理（NFR-05 / AC-22）。

需求原文：
    **NFR-05** 交易、配置、风控与审计记录建议保留至少 1 年；原始行情短期保留 7 天，可按空间调整；
    按策略决策保存的必要行情快照随审计记录保留。磁盘容量设告警及暂停阈值。
    达到容量边界时停止新增风险，不静默丢弃账务记录。

清理边界 —— 只删**可重建的原始行情**，不碰账务与审计：

    * ``quote_snapshot`` 中**没有被任何交易意图引用**的行属于原始行情，
      超过 ``raw_market_data_retention_days`` 后删除；
    * 被 ``trade_intent.quote_snapshot_id`` 引用的快照是决策证据，
      随审计记录保留，永不删除 —— 外键是 ``ON DELETE SET NULL``，
      删了会静默抹掉决策与行情的对应关系；
    * 账本、成交、订单、意图、预留、风控决定、现金流、权益快照、
      账户快照、暂停与告警**一律不删**：账务记录不因为磁盘紧张而被丢弃。

删除按小批次分批提交，避免长事务锁住表。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import delete, func, select

from bybitbot.clock import now_utc
from bybitbot.db.models import QuoteSnapshot, TradeIntent


@dataclass(frozen=True)
class RetentionReport:
    """一次清理的结果。"""

    ran_at: dt.datetime
    raw_market_data_deleted: int
    raw_market_data_remaining: int
    decision_snapshots_kept: int
    cutoff: dt.datetime

    def as_dict(self) -> dict[str, object]:
        return {
            "ran_at": self.ran_at.isoformat(),
            "cutoff": self.cutoff.isoformat(),
            "raw_market_data_deleted": self.raw_market_data_deleted,
            "raw_market_data_remaining": self.raw_market_data_remaining,
            "decision_snapshots_kept": self.decision_snapshots_kept,
        }


class RetentionCleaner:
    """原始行情清理器。

    Args:
        sessionmaker: 数据库会话工厂。
        environment: 只清理本环境的数据。
        raw_market_data_days: 原始行情保留天数（NFR-05 默认 7 天）。
        retention_days: 账务与审计记录的保留下限；清理器不会删除它们，
            这个值只用于在报告里说明账务记录仍在保留期内。
        batch_size: 单批删除的行数。
    """

    def __init__(
        self,
        sessionmaker,
        environment: str,
        *,
        raw_market_data_days: int = 7,
        retention_days: int = 365,
        batch_size: int = 500,
    ) -> None:
        if raw_market_data_days < 1:
            raise ValueError("原始行情保留天数必须为正数")
        if batch_size < 1:
            raise ValueError("删除批次必须为正数")
        self.sessionmaker = sessionmaker
        self.environment = environment
        self.raw_market_data_days = raw_market_data_days
        self.retention_days = retention_days
        self.batch_size = batch_size

    async def clean(self, *, now: dt.datetime | None = None) -> RetentionReport:
        """清理过期原始行情，返回结果报告。

        幂等：重复调用不会重复删除，也不会影响已保留的决策快照。
        """
        moment = now or now_utc()
        cutoff = moment - dt.timedelta(days=self.raw_market_data_days)
        async with self.sessionmaker() as session:
            referenced = set(
                await session.scalars(
                    select(TradeIntent.quote_snapshot_id)
                    .where(
                        TradeIntent.environment == self.environment,
                        TradeIntent.quote_snapshot_id.isnot(None),
                    )
                    .distinct()
                )
            )
        deleted = 0
        while True:
            async with self.sessionmaker() as session:
                statement = (
                    select(QuoteSnapshot.id)
                    .where(
                        QuoteSnapshot.environment == self.environment,
                        QuoteSnapshot.received_at < cutoff,
                    )
                    .order_by(QuoteSnapshot.received_at)
                    .limit(self.batch_size)
                )
                if referenced:
                    statement = statement.where(QuoteSnapshot.id.notin_(referenced))
                batch = list(await session.scalars(statement))
                if not batch:
                    break
                await session.execute(delete(QuoteSnapshot).where(QuoteSnapshot.id.in_(batch)))
                await session.commit()
            deleted += len(batch)
            if len(batch) < self.batch_size:
                break
        async with self.sessionmaker() as session:
            remaining = await session.scalar(
                select(func.count())
                .select_from(QuoteSnapshot)
                .where(QuoteSnapshot.environment == self.environment)
            )
        return RetentionReport(
            ran_at=moment,
            raw_market_data_deleted=deleted,
            raw_market_data_remaining=int(remaining or 0),
            decision_snapshots_kept=len(referenced),
            cutoff=cutoff,
        )

    async def accounting_retention_deadline(self, *, now: dt.datetime | None = None) -> dt.datetime:
        """账务与审计记录的保留下限时刻。

        清理器不会删除账务记录；这个时刻是"早于它就该报警而不是清理"的分界。
        """
        moment = now or now_utc()
        return moment - dt.timedelta(days=self.retention_days)


__all__ = ["RetentionCleaner", "RetentionReport"]
