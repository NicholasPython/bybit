"""执行者租约（EXE-05）。

需求原文：
    每个账户/环境最多一个具有发单资格的执行者。部署、重启、锁丢失和数据库断连时
    执行资格失效。新执行者接管前必须确认旧执行者已停止并完成对账；
    **仅数据库锁超时不能作为旧进程已停止的证据**。失权时的在途请求作为待核实交易处理。

实现要点
--------
互斥用 **Postgres 会话级咨询锁**（``pg_try_advisory_lock``）：
    * 会话级锁随连接断开自动释放 —— 数据库断连时执行资格立即失效；
    * 事务级锁（``pg_advisory_xact_lock``）在提交时释放，无法覆盖长生命周期
      的执行者，因此不能用于这里。

``hashtext`` 把长字符串压成一个 32 位键空间；账户数量很小，冲突概率可接受，
且冲突只造成过度互斥（安全方向）。

接管流程
--------
``try_acquire`` 只回答「锁现在能不能拿到」。它**不能**证明旧执行者已停止 ——
锁可能因为网络分区、旧进程卡死或数据库重启而释放。因此：

    1. 拿到锁 → 进入 ``NEEDS_CONFIRMATION``；
    2. 必须由运维通过控制命令显式确认旧执行者已停止并完成对账
       （``confirm_takeover``）；
    3. 才进入 ``ELIGIBLE``，获得发单资格。

失权（``holder`` 之外的路径）时，所有在途请求按「待核实交易」处理，
由对账流程消除 UNKNOWN，不允许换编号重发。
"""

from __future__ import annotations

import contextlib
import datetime as dt
import logging
import uuid
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession

from bybitbot.clock import now_utc

logger = logging.getLogger(__name__)


class LeaseState(StrEnum):
    """执行者资格状态。"""

    #: 没有持锁者。
    FREE = "FREE"
    #: 本进程持锁，但接管尚未被人工确认。
    NEEDS_CONFIRMATION = "NEEDS_CONFIRMATION"
    #: 本进程持锁且接管已确认 —— 有发单资格。
    ELIGIBLE = "ELIGIBLE"
    #: 锁丢失或数据库断连 —— 资格失效。
    LOST = "LOST"
    #: 被别的进程持锁。
    HELD_BY_OTHER = "HELD_BY_OTHER"


@dataclass
class LeaseStatus:
    """租约状态快照。"""

    state: LeaseState
    holder_id: str
    detail: str = ""

    @property
    def can_send_orders(self) -> bool:
        return self.state is LeaseState.ELIGIBLE


class ExecutorLease:
    """账户/环境级执行者租约。

    持有一个专用的数据库连接；连接断开即视为失权。
    """

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        environment: str,
        account_uid: str,
        holder_id: str | None = None,
    ) -> None:
        self.engine = engine
        self.environment = environment
        self.account_uid = account_uid
        self.holder_id = holder_id or f"{uuid.getnode():x}-{uuid.uuid4().hex[:12]}"
        self._connection: AsyncConnection | None = None
        self._state = LeaseState.FREE
        self._detail = ""
        self._confirmed = False
        self._audit_row_id: int | None = None

    @property
    def key(self) -> str:
        return f"{self.environment}:executor:{self.account_uid}"

    @property
    def state(self) -> LeaseState:
        return self._state

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def try_acquire(self) -> LeaseStatus:
        """尝试取得锁。

        取得后进入 ``NEEDS_CONFIRMATION``；必须经 :meth:`confirm_takeover`
        才具备发单资格（EXE-05）。
        """
        if self._connection is not None and self._state in {
            LeaseState.NEEDS_CONFIRMATION,
            LeaseState.ELIGIBLE,
        }:
            return await self.heartbeat()
        try:
            await self._drop_connection()
            self._confirmed = False
            self._connection = await self.engine.connect()
            result = await self._connection.execute(
                text("SELECT pg_try_advisory_lock(hashtext(:key))"),
                {"key": self.key},
            )
            acquired = bool(result.scalar())
        except Exception as exc:  # 数据库断连 —— 资格失效
            self._state = LeaseState.LOST
            self._detail = f"获取租约失败：{exc}"
            await self._drop_connection()
            return LeaseStatus(self._state, self.holder_id, self._detail)

        if not acquired:
            await self._drop_connection()
            self._state = LeaseState.HELD_BY_OTHER
            self._detail = "锁被其它执行者持有"
            return LeaseStatus(self._state, self.holder_id, self._detail)

        self._state = LeaseState.NEEDS_CONFIRMATION
        self._detail = "已持锁，等待确认旧执行者已停止并完成对账"
        self._audit_row_id = None
        try:
            await self._record_lease(released_at=None, note=self._detail)
        except Exception:
            self._state = LeaseState.LOST
            self._detail = "租约审计不可写，禁止获得执行资格"
            await self._drop_connection()
        return LeaseStatus(self._state, self.holder_id, self._detail)

    async def confirm_takeover(self, *, operator: str, evidence: str) -> LeaseStatus:
        """人工确认接管。

        Args:
            operator: 操作者标识。
            evidence: 确认依据（审计留痕）。
        """
        if self._state not in {LeaseState.NEEDS_CONFIRMATION, LeaseState.ELIGIBLE}:
            raise RuntimeError(f"当前租约状态 {self._state} 不能确认接管")
        if not operator.strip() or not evidence.strip():
            raise RuntimeError("接管需要操作者与核对证据")
        status = await self.heartbeat()
        if status.state not in {LeaseState.NEEDS_CONFIRMATION, LeaseState.ELIGIBLE}:
            raise RuntimeError(f"租约连接或锁已失效：{status.detail}")
        self._confirmed = True
        self._state = LeaseState.ELIGIBLE
        self._detail = f"由 {operator} 确认接管：{evidence}"
        try:
            await self._record_lease(released_at=None, note=self._detail)
        except Exception:
            self._state = LeaseState.LOST
            await self._drop_connection()
            raise
        return LeaseStatus(self._state, self.holder_id, self._detail)

    async def heartbeat(self) -> LeaseStatus:
        """心跳。锁丢失或连接断开时把资格置为 LOST。"""
        if (
            self._connection is None
            or self._connection.closed
            or self._connection.invalidated
            or self._state not in {LeaseState.NEEDS_CONFIRMATION, LeaseState.ELIGIBLE}
        ):
            self._state = LeaseState.LOST
            self._detail = "没有有效持锁连接"
            await self._drop_connection()
            return LeaseStatus(self._state, self.holder_id, self._detail)
        try:
            # 禁止在失效连接上透明重连后误把 SELECT 1 成功当成仍然持锁。
            held = await self._connection.scalar(
                text(
                    "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' "
                    "AND pid = pg_backend_pid() AND granted "
                    "AND classid::bigint = ((hashtext(:key)::bigint >> 32) & 4294967295) "
                    "AND objid::bigint = (hashtext(:key)::bigint & 4294967295) AND objsubid = 1)"
                ),
                {"key": self.key},
            )
            if not held:
                raise RuntimeError("咨询锁已丢失")
            await self._record_lease(released_at=None, note=None)
        except Exception as exc:
            self._state = LeaseState.LOST
            self._detail = f"心跳失败（数据库断连）：{exc}"
            await self._drop_connection()
        return LeaseStatus(self._state, self.holder_id, self._detail)

    async def release(self) -> None:
        """先记录释放，再关闭持锁会话；未持锁实例不生成释放记录。"""
        self._state = LeaseState.FREE
        self._confirmed = False
        self._detail = "已释放"
        try:
            if self._audit_row_id is not None:
                await self._record_lease(released_at=now_utc(), note="正常释放")
        finally:
            await self._drop_connection()
            self._audit_row_id = None

    async def _drop_connection(self) -> None:
        if self._connection is not None:
            # 会话锁不会随 rollback 释放。先 invalidate，防止连接池复用持锁连接。
            with contextlib.suppress(Exception):
                await self._connection.invalidate()
            with contextlib.suppress(Exception):
                await self._connection.close()
            self._connection = None

    # ------------------------------------------------------------------
    # 审计
    # ------------------------------------------------------------------

    async def _record_lease(self, *, released_at: dt.datetime | None, note: str | None) -> None:
        """一次持锁对应一行；审计不可写时调用者必须使执行资格失效。"""
        from bybitbot.db.models import ExecutorLease as ExecutorLeaseRow

        async with AsyncSession(self.engine, expire_on_commit=False) as session:
            moment = now_utc()
            if self._audit_row_id is None:
                row = ExecutorLeaseRow(
                    environment=self.environment,
                    account_uid=self.account_uid,
                    holder_id=self.holder_id,
                    acquired_at=moment,
                    released_at=released_at,
                    heartbeat_at=moment,
                )
                session.add(row)
            else:
                row = await session.get(ExecutorLeaseRow, self._audit_row_id)
                if row is None or row.holder_id != self.holder_id:
                    raise RuntimeError("当前执行者的租约审计记录缺失")
                row.heartbeat_at = moment
                row.released_at = released_at
            await session.commit()
            self._audit_row_id = row.id


def same_holder(a: str, b: str) -> bool:
    return a == b


__all__ = ["ExecutorLease", "LeaseState", "LeaseStatus", "same_holder"]
