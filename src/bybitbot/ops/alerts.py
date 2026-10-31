"""告警（OPS-04）。

需求原文：
    对账户异常、未知订单、对账差异、断流、API 限流/权限失败、退出未完成、
    到期未结算、磁盘/数据库故障产生告警；告警去重、保留首发与恢复事件。
    通知失败落库并重试；**关键告警超时未送达时暂停新增风险**。

三条规则：
    * **去重** —— 同一 ``alert_key`` 只保留一条记录，反复触发只更新
      ``first_seen_at`` 与证据，不重复刷屏。
    * **保留首发与恢复事件** —— ``first_seen_at`` 不因重复触发而改变；
      解决时写 ``resolved_at``。
    * **送达失败要落库** —— 失败写 ``last_delivery_error``、累加
      ``delivery_attempts``，并把「关键告警未送达」变成一个暂停。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import select

from bybitbot.clock import now_utc
from bybitbot.db.models import Alert


class Severity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class AlertStatus(StrEnum):
    ACTIVE = "ACTIVE"
    DELIVERED = "DELIVERED"
    RESOLVED = "RESOLVED"


@dataclass
class AlertView:
    """告警视图。"""

    id: int
    alert_key: str
    severity: str
    title: str
    detail: str
    status: str
    first_seen_at: dt.datetime
    resolved_at: dt.datetime | None
    delivery_attempts: int
    last_delivery_error: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "alert_key": self.alert_key,
            "severity": self.severity,
            "title": self.title,
            "detail": self.detail,
            "status": self.status,
            "first_seen_at": self.first_seen_at.isoformat(),
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "delivery_attempts": self.delivery_attempts,
            "last_delivery_error": self.last_delivery_error,
        }


class AlertManager:
    """告警去重与送达状态管理。"""

    def __init__(self, sessionmaker, environment: str) -> None:
        self.sessionmaker = sessionmaker
        self.environment = environment

    async def raise_alert(
        self,
        *,
        alert_key: str,
        severity: str,
        title: str,
        detail: str = "",
        context: dict[str, object] | None = None,
    ) -> Alert:
        """登记一条告警。重复 ``alert_key`` 不会新建记录。"""
        async with self.sessionmaker() as session:
            existing = (
                await session.execute(
                    select(Alert).where(
                        Alert.environment == self.environment,
                        Alert.alert_key == alert_key,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                # 保留首发时间；更新细节与上下文。
                existing.detail = detail or existing.detail
                if existing.status == AlertStatus.RESOLVED:
                    existing.status = AlertStatus.ACTIVE
                    events = list((existing.context or {}).get("history", []))
                    reopened_at = now_utc().isoformat()
                    events.append({"kind": "REOPENED", "at": reopened_at})
                    existing.context = {
                        **(existing.context or {}),
                        "history": events,
                        "episode_started_at": reopened_at,
                        "last_delivery_at": None,
                        "delivered_channels": {},
                    }
                existing.resolved_at = None
                if context:
                    merged = dict(existing.context or {})
                    merged.update(context)
                    existing.context = merged
                await session.commit()
                await session.refresh(existing)
                return existing

            alert = Alert(
                environment=self.environment,
                alert_key=alert_key,
                severity=severity,
                title=title,
                detail=detail,
                status=AlertStatus.ACTIVE,
                first_seen_at=now_utc(),
                delivery_attempts=0,
                context=context or {},
            )
            session.add(alert)
            await session.commit()
            await session.refresh(alert)
            return alert

    async def mark_delivered(self, alert_id: int) -> None:
        async with self.sessionmaker() as session:
            alert = await session.get(Alert, alert_id)
            if alert is None:
                return
            alert.delivery_attempts += 1
            alert.last_delivery_error = None
            alert.context = {**(alert.context or {}), "last_delivery_at": now_utc().isoformat()}
            alert.status = AlertStatus.DELIVERED
            await session.commit()

    async def mark_delivery_failed(self, alert_id: int, error: str) -> None:
        """送达失败：落库并保留错误，供重试。"""
        async with self.sessionmaker() as session:
            alert = await session.get(Alert, alert_id)
            if alert is None:
                return
            alert.delivery_attempts += 1
            alert.last_delivery_error = error[:2000]
            alert.context = {**(alert.context or {}), "last_delivery_at": now_utc().isoformat()}
            alert.status = AlertStatus.ACTIVE
            await session.commit()

    async def resolve(self, alert_key: str, *, detail: str = "") -> Alert | None:
        """告警恢复（保留首发与恢复两个事件）。"""
        async with self.sessionmaker() as session:
            alert = (
                await session.execute(
                    select(Alert).where(
                        Alert.environment == self.environment,
                        Alert.alert_key == alert_key,
                    )
                )
            ).scalar_one_or_none()
            if alert is None or alert.status == AlertStatus.RESOLVED:
                return alert
            alert.status = AlertStatus.RESOLVED
            alert.resolved_at = now_utc()
            events = list((alert.context or {}).get("history", []))
            events.append(
                {"kind": "RESOLVED", "at": alert.resolved_at.isoformat(), "detail": detail}
            )
            alert.context = {**(alert.context or {}), "history": events}
            if detail:
                alert.detail = detail
            await session.commit()
            await session.refresh(alert)
            return alert

    async def undelivered(self, *, severity: str | None = None) -> list[Alert]:
        """尚未送达的告警（供重试与「未送达即暂停」的判定）。"""
        async with self.sessionmaker() as session:
            stmt = select(Alert).where(
                Alert.environment == self.environment,
                Alert.status == AlertStatus.ACTIVE,
            )
            if severity is not None:
                stmt = stmt.where(Alert.severity == severity)
            return list(await session.scalars(stmt))

    async def active(self) -> list[AlertView]:
        async with self.sessionmaker() as session:
            rows = list(
                await session.scalars(
                    select(Alert)
                    .where(
                        Alert.environment == self.environment,
                        Alert.status != AlertStatus.RESOLVED,
                    )
                    .order_by(Alert.first_seen_at.desc())
                )
            )
        return [
            AlertView(
                id=r.id,
                alert_key=r.alert_key,
                severity=r.severity,
                title=r.title,
                detail=r.detail,
                status=r.status,
                first_seen_at=r.first_seen_at,
                resolved_at=r.resolved_at,
                delivery_attempts=r.delivery_attempts,
                last_delivery_error=r.last_delivery_error,
            )
            for r in rows
        ]


__all__ = ["AlertManager", "AlertStatus", "AlertView", "Severity"]
