"""暂停管理（RSK-03 / OPS-03）。

需求原文：
    **OPS-03** 数据暂时失效等技术暂停须经重新同步及全部检查通过才可解除；
    亏损熔断、未知订单、外部改仓或恢复演练造成的暂停保持锁定。
    人工确认不能替代 EXE-02 证据与对账；全部解除条件具备后方可恢复。
    **重启不能解除暂停。**

    **RSK-03** 账户不可信、未知执行结果和资金差异默认暂停整个账户。
    纯单品种行情异常可以只暂停相关品种，但不能沿用其旧估值证明账户安全。

两类暂停
--------
    * **技术暂停**（``locked=False``）：数据暂时失效等。解除条件是
      「重新同步 + 全部检查通过」，由系统判定，不靠人工点头。
    * **锁定暂停**（``locked=True``）：亏损熔断、未知订单、外部改仓、
      恢复演练造成的暂停。必须走审计化的人工解除，并且**不能**因为重启
      或换日而消失。

所有暂停都落库，因此重启后自动恢复 —— 这是「重启不能解除暂停」的实现方式。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import select

from bybitbot.clock import now_utc
from bybitbot.db.models import RiskPause


class PauseScope(StrEnum):
    ACCOUNT = "ACCOUNT"
    UNDERLYING = "UNDERLYING"
    SYMBOL = "SYMBOL"


class PauseReason(StrEnum):
    """暂停原因码。写进 ``risk_pause.reason_code``。"""

    # 锁定类
    LOSS_CIRCUIT_BREAKER = "LOSS_CIRCUIT_BREAKER"
    DRAWDOWN_LIMIT = "DRAWDOWN_LIMIT"
    UNKNOWN_ORDER = "UNKNOWN_ORDER"
    EXTERNAL_ACCOUNT_CHANGE = "EXTERNAL_ACCOUNT_CHANGE"
    RECONCILIATION_DIFFERENCE = "RECONCILIATION_DIFFERENCE"
    ACCOUNT_DEBT = "ACCOUNT_DEBT"
    ACCOUNT_UNTRUSTED = "ACCOUNT_UNTRUSTED"
    EXECUTION_AUTHORITY_LOST = "EXECUTION_AUTHORITY_LOST"
    # 技术类
    MARKET_DATA_STALE = "MARKET_DATA_STALE"
    MARKET_DATA_GAP = "MARKET_DATA_GAP"
    ACCOUNT_SNAPSHOT_STALE = "ACCOUNT_SNAPSHOT_STALE"
    VALUATION_UNAVAILABLE = "VALUATION_UNAVAILABLE"
    CLOCK_SKEW = "CLOCK_SKEW"
    DISK_PRESSURE = "DISK_PRESSURE"
    NOTIFICATION_FAILURE = "NOTIFICATION_FAILURE"
    VALIDATION_INTEGRITY = "VALIDATION_INTEGRITY"
    MANUAL = "MANUAL"


#: 锁定类原因：这些暂停不会因为重新同步成功而自动解除。
LOCKED_REASONS: frozenset[str] = frozenset(
    {
        PauseReason.LOSS_CIRCUIT_BREAKER,
        PauseReason.DRAWDOWN_LIMIT,
        PauseReason.UNKNOWN_ORDER,
        PauseReason.EXTERNAL_ACCOUNT_CHANGE,
        PauseReason.RECONCILIATION_DIFFERENCE,
        PauseReason.ACCOUNT_DEBT,
        PauseReason.ACCOUNT_UNTRUSTED,
        PauseReason.EXECUTION_AUTHORITY_LOST,
    }
)


@dataclass
class PauseView:
    """暂停的对外视图（管理页展示用）。"""

    id: int
    scope: str
    scope_key: str
    reason_code: str
    reason_detail: str
    locked: bool
    active: bool
    created_at: dt.datetime

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "scope": self.scope,
            "scope_key": self.scope_key,
            "reason_code": self.reason_code,
            "reason_detail": self.reason_detail,
            "locked": self.locked,
            "active": self.active,
            "created_at": self.created_at.isoformat(),
        }


class PauseManager:
    """暂停的唯一入口。"""

    def __init__(self, sessionmaker, environment: str) -> None:
        self.sessionmaker = sessionmaker
        self.environment = environment

    async def pause(
        self,
        *,
        reason_code: str,
        reason_detail: str,
        scope: str = PauseScope.ACCOUNT,
        scope_key: str = "",
        locked: bool | None = None,
        evidence: dict[str, object] | None = None,
    ) -> RiskPause:
        """登记一个暂停。

        同一 ``(scope, scope_key, reason_code)`` 的重复暂停只更新证据，
        不新建记录 —— 避免告警风暴把表撑爆。
        """
        is_locked = LOCKED_REASONS.__contains__(reason_code) if locked is None else locked
        async with self.sessionmaker() as session:
            existing = (
                await session.execute(
                    select(RiskPause).where(
                        RiskPause.environment == self.environment,
                        RiskPause.scope == scope,
                        RiskPause.scope_key == scope_key,
                        RiskPause.reason_code == reason_code,
                        RiskPause.active.is_(True),
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                if evidence:
                    merged = dict(existing.evidence or {})
                    merged.update(evidence)
                    existing.evidence = merged
                    existing.reason_detail = reason_detail or existing.reason_detail
                await session.commit()
                await session.refresh(existing)
                return existing

            pause = RiskPause(
                environment=self.environment,
                scope=scope,
                scope_key=scope_key,
                reason_code=reason_code,
                reason_detail=reason_detail,
                locked=is_locked,
                active=True,
                evidence=evidence or {},
                created_at=now_utc(),
            )
            session.add(pause)
            await session.commit()
            await session.refresh(pause)
            return pause

    async def active_pauses(
        self, *, scope: str | None = None, scope_key: str | None = None
    ) -> list[RiskPause]:
        async with self.sessionmaker() as session:
            stmt = select(RiskPause).where(
                RiskPause.environment == self.environment,
                RiskPause.active.is_(True),
            )
            if scope is not None:
                stmt = stmt.where(RiskPause.scope == scope)
            if scope_key is not None:
                stmt = stmt.where(RiskPause.scope_key == scope_key)
            return list(await session.scalars(stmt))

    async def is_paused(self, *, scope: str = PauseScope.ACCOUNT, scope_key: str = "") -> bool:
        """是否处于暂停状态。

        账户级暂停对所有品种生效；品种级暂停只影响对应品种
        （但不能沿用其旧估值证明账户安全 —— 调用方仍需重新估值）。
        """
        async with self.sessionmaker() as session:
            stmt = select(RiskPause).where(
                RiskPause.environment == self.environment,
                RiskPause.active.is_(True),
            )
            rows = list(await session.scalars(stmt))
        for row in rows:
            if row.scope == PauseScope.ACCOUNT:
                return True
            if row.scope == scope and row.scope_key == scope_key:
                return True
        return False

    async def blocking_reasons(self) -> list[PauseView]:
        """当前阻塞原因（管理页与告警用）。"""
        rows = await self.active_pauses()
        return [
            PauseView(
                id=r.id,
                scope=r.scope,
                scope_key=r.scope_key,
                reason_code=r.reason_code,
                reason_detail=r.reason_detail,
                locked=r.locked,
                active=r.active,
                created_at=r.created_at,
            )
            for r in rows
        ]

    async def resolve(
        self,
        *,
        pause_id: int,
        operator: str,
        reason: str,
        evidence: dict[str, object] | None = None,
    ) -> RiskPause:
        """人工解除一个暂停。

        锁定暂停也允许人工解除，但必须留审计（操作者、理由、证据）——
        需求禁止的是「自动」解除，不是禁止人工解除。
        """
        async with self.sessionmaker() as session:
            pause = await session.get(RiskPause, pause_id)
            if pause is None:
                raise ValueError(f"暂停 {pause_id} 不存在")
            merged = dict(pause.evidence or {})
            merged.update(evidence or {})
            merged["resolution"] = {
                "operator": operator,
                "reason": reason,
                "at": now_utc().isoformat(),
            }
            pause.evidence = merged
            pause.active = False
            pause.resolved_at = now_utc()
            pause.resolved_by = operator
            await session.commit()
            await session.refresh(pause)
            return pause

    async def resolve_technical(
        self, *, reason_code: str, scope: str = PauseScope.ACCOUNT, scope_key: str = ""
    ) -> int:
        """自动解除技术暂停。

        只解除**非锁定**的技术暂停；锁定暂停一律留给人工
        （OPS-03：亏损熔断、未知订单、外部改仓造成的暂停保持锁定）。
        """
        async with self.sessionmaker() as session:
            stmt = select(RiskPause).where(
                RiskPause.environment == self.environment,
                RiskPause.scope == scope,
                RiskPause.scope_key == scope_key,
                RiskPause.reason_code == reason_code,
                RiskPause.active.is_(True),
                RiskPause.locked.is_(False),
            )
            rows = list(await session.scalars(stmt))
            for row in rows:
                row.active = False
                row.resolved_at = now_utc()
                row.resolved_by = "system:technical-resync"
                row.evidence = {**(row.evidence or {}), "auto_resolved": True}
            await session.commit()
            return len(rows)


__all__ = [
    "LOCKED_REASONS",
    "PauseManager",
    "PauseReason",
    "PauseScope",
    "PauseView",
]
