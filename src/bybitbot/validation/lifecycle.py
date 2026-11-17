"""Run lifecycle: freeze, heartbeat, mutual exclusion, reports and export.

Everything is anchored to the study row and its append-only ledger. ``ended`` is
a terminal bookkeeping state: it never invents settlement or clears exposure.
The current checkout is verified before a run may continue, so a source change
fail-closed rather than silently running under a different engine.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal

from sqlalchemy import select

from bybitbot.clock import now_utc
from bybitbot.validation.archive import (
    archive_bytes,
    digest,
    freeze_sources,
    verify_sources,
)
from bybitbot.validation.journal import append_event, checkpoint, verify
from bybitbot.validation.models import (
    ValidationLedger,
    ValidationObservation,
    ValidationPosition,
    ValidationStudy,
)
from bybitbot.validation.sampling import json_value

RUN_ID_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}\Z")


def _now_ms() -> int:
    return int(now_utc().timestamp() * 1000)


class ValidationLifecycle:
    def __init__(self, store):
        # 服务传入 ValidationStore；操作台只读场景可以传 Database，
        # 因为 status/export 自行接收环境/账户且永不执行交易。
        if hasattr(store, "active") and hasattr(store, "locked"):
            self.store = store
        else:
            self.store = type("_ReadOnlyStore", (), {"sessionmaker": store.sessionmaker})()

    @property
    def sessionmaker(self):
        return self.store.sessionmaker

    async def freeze(self, study_id: int) -> dict:
        """Pin protocol, sources, config fingerprint and strategy bindings once.

        Sources are frozen from the current checkout. Verification on every tick
        compares the current checkout byte-for-byte, so no silent engine upgrade
        can change the meaning of a running study.
        """
        async with self.sessionmaker() as session:
            study = await self.store.locked(session, study_id)
            if "protocol_hash" in study.evidence:
                if study.evidence["protocol_hash"] != digest(study.protocol):
                    raise ValueError("验证协议哈希不符")
                return study.evidence
            manifest = freeze_sources()
            positions = list(
                await session.scalars(
                    select(ValidationPosition).where(ValidationPosition.study_id == study.id)
                )
            )
            bindings = sorted(
                {
                    json.dumps(json_value(row.state["binding"]), sort_keys=True)
                    for row in positions
                    if isinstance(row.state, dict) and row.state.get("binding")
                }
            )
            bindings = [json.loads(item) for item in bindings]
            evidence = {
                "protocol_hash": digest(study.protocol),
                "sources": manifest["sources"],
                "bindings": bindings,
            }
            study.evidence = study.evidence | evidence
            await append_event(
                session,
                study,
                dedupe_key="freeze",
                kind="freeze",
                occurred_ms=_now_ms(),
                payload={
                    "protocol_hash": evidence["protocol_hash"],
                    "source_count": len(manifest["sources"]),
                    "bindings": bindings,
                },
            )
            await session.commit()
            return evidence

    async def register_exclusion(self, study_id: int, *, other_run_ids=()) -> dict:
        """Record explicit mutual exclusion with opposing validation studies.

        ``other_run_ids`` names runs whose protocol must be hashed differently.
        Every named run must exist for this account and be either ended or
        irreconcilable; a missing or mismatched study fails closed.
        """
        if not isinstance(other_run_ids, (tuple, list)) or len(other_run_ids) > 10:
            raise ValueError("互斥运行编号最多 10 个")
        async with self.sessionmaker() as session:
            study = await self.store.locked(session, study_id)
            if study.status == "ended":
                raise ValueError("已结束的运行不能修改互斥登记")
            mine = digest(study.protocol)
            proof = []
            for run_id in other_run_ids:
                if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
                    raise ValueError("非法互斥运行编号")
                other = await session.scalar(
                    select(ValidationStudy).where(
                        ValidationStudy.environment == study.environment,
                        ValidationStudy.account_uid == study.account_uid,
                        ValidationStudy.run_id == run_id,
                    )
                )
                if (
                    other is None
                    or other.id == study.id
                    or other.direction == study.direction
                    or other.policy == study.policy
                    or digest(other.protocol) == mine
                ):
                    raise ValueError("互斥运行不存在或协议并非对向口径")
                proof.append(
                    {
                        "run_id": run_id,
                        "direction": other.direction,
                        "policy": other.policy,
                        "protocol_hash": digest(other.protocol),
                        "status": other.status,
                        "chain_head": other.chain_head,
                    }
                )
            existing = study.evidence.get("exclusion")
            if existing is not None and existing != proof:
                raise ValueError("互斥登记已绑定其他运行")
            if existing is None:
                study.evidence = study.evidence | {"exclusion": proof}
                await append_event(
                    session,
                    study,
                    dedupe_key="exclusion",
                    kind="exclusion",
                    occurred_ms=_now_ms(),
                    payload={"opposing": proof},
                )
                await session.commit()
            return {"opposing": proof}

    async def check(self, study_id: int) -> None:
        """Fail-closed preconditions: source identity, chain, manifest and exclusion."""
        async with self.sessionmaker() as session:
            study = await self.store.locked(session, study_id)
            evidence = study.evidence
            if evidence.get("protocol_hash") != digest(study.protocol):
                raise ValueError("验证协议哈希不符")
            if "sources" not in evidence:
                raise ValueError("验证源码尚未冻结")
            verify_sources({"sources": evidence["sources"]})
            positions = list(
                await session.scalars(
                    select(ValidationPosition).where(ValidationPosition.study_id == study.id)
                )
            )
            for row in positions:
                binding = row.state.get("binding") if isinstance(row.state, dict) else None
                if binding and binding not in evidence.get("bindings", []):
                    raise ValueError("策略版本与冻结清单不符")
            for other in evidence.get("exclusion", []):
                if not isinstance(other, dict) or not RUN_ID_RE.fullmatch(
                    str(other.get("run_id", ""))
                ):
                    raise ValueError("互斥登记损坏")
                peer = await session.scalar(
                    select(ValidationStudy).where(
                        ValidationStudy.environment == study.environment,
                        ValidationStudy.account_uid == study.account_uid,
                        ValidationStudy.run_id == other["run_id"],
                    )
                )
                if peer is None or digest(peer.protocol) != other.get("protocol_hash"):
                    raise ValueError("互斥运行哈希不符")
                if (
                    peer.status != other.get("status")
                    or peer.chain_head != other.get("chain_head")
                    or peer.direction != other.get("direction")
                    or peer.policy != other.get("policy")
                ):
                    raise ValueError("互斥运行状态已改变，需要重新登记")
            await verify(session, study)
            checkpoint_event = await session.scalar(
                select(ValidationLedger).where(
                    ValidationLedger.study_id == study.id,
                    ValidationLedger.kind == "checkpoint",
                )
            )
            if checkpoint_event is None:
                raise ValueError("验证检查点未锚定到账本")

    async def checkpoint(self, study_id: int) -> None:
        """Seal current state into the chain; a failing verification aborts the tick."""
        async with self.sessionmaker() as session:
            study = await self.store.locked(session, study_id)
            await checkpoint(session, study, now_ms=_now_ms())
            await session.commit()

    async def heartbeat(self, study_id: int, *, status: str | None = None) -> None:
        """Append a status transition and move the heartbeat to the present."""
        if status is not None and status not in {"running", "ending", "ended"}:
            raise ValueError("非法运行状态")
        async with self.sessionmaker() as session:
            study = await self.store.locked(session, study_id)
            if study.status == "ended":
                return
            now = _now_ms()
            if status is not None and status != study.status:
                study.status = status
                await append_event(
                    session,
                    study,
                    dedupe_key=f"status:{status}",
                    kind="status",
                    occurred_ms=now,
                    payload={"status": status, "at_ms": now},
                )
            study.heartbeat_ms = now
            await session.commit()

    async def end(self, study_id: int) -> dict:
        """Mark a run ended, preserve unresolved positions and seal the evidence."""
        async with self.sessionmaker() as session:
            study = await self.store.locked(session, study_id)
            if study.status == "ended":
                return {"run_id": study.run_id, "status": study.status}
            if "sources" not in study.evidence:
                raise ValueError("运行未冻结源码，不能结束")
            positions = list(
                await session.scalars(
                    select(ValidationPosition).where(ValidationPosition.study_id == study.id)
                )
            )
            unresolved = []
            for row in positions:
                state = row.state if isinstance(row.state, dict) else {}
                if "unresolved" in row.phase or any(
                    Decimal(str(state.get(key, 0) or 0)) != 0
                    for key in ("long_qty", "short_qty", "reserved_risk")
                ):
                    unresolved.append(row.base)
                    if "unresolved" not in row.phase:
                        row.phase = "study_ended_unresolved"
                        row.state = state | {"unresolved_risk": True}
            study.status = "ended"
            study.end_ms = min(study.end_ms, _now_ms())
            study.heartbeat_ms = study.end_ms
            await append_event(
                session,
                study,
                dedupe_key="ended",
                kind="ended",
                occurred_ms=study.end_ms,
                payload={"unresolved_bases": unresolved, "chain_head": study.chain_head},
            )
            # 终态检查点必须最后写，保证归档里的最后一条事件锚定最终状态。
            await checkpoint(session, study, now_ms=study.end_ms)
            await session.commit()
            return {
                "run_id": study.run_id,
                "status": study.status,
                "unresolved_bases": unresolved,
                "final_report_ready": True,
            }

    async def status(self, database, *, environment: str, account_uid: str):
        """Read-only summary for the console; never exports frozen source content."""
        if not environment or not account_uid:
            raise ValueError("验证状态必须限定环境与账户")
        result = []
        async with database.sessionmaker() as session:
            studies = list(
                await session.scalars(
                    select(ValidationStudy)
                    .where(
                        ValidationStudy.environment == environment,
                        ValidationStudy.account_uid == account_uid,
                    )
                    .order_by(ValidationStudy.id.desc())
                    .limit(100)
                )
            )
            for study in studies:
                positions = list(
                    await session.scalars(
                        select(ValidationPosition).where(ValidationPosition.study_id == study.id)
                    )
                )
                observations = list(
                    await session.scalars(
                        select(ValidationObservation).where(
                            ValidationObservation.study_id == study.id
                        )
                    )
                )
                events = list(
                    await session.scalars(
                        select(ValidationLedger)
                        .where(ValidationLedger.study_id == study.id)
                        .order_by(ValidationLedger.sequence)
                    )
                )
                result.append(
                    {
                        "run_id": study.run_id,
                        "environment": study.environment,
                        "account_uid": study.account_uid,
                        "status": study.status,
                        "policy": study.policy,
                        "direction": study.direction,
                        "bases": list(study.bases),
                        "started_ms": study.started_ms,
                        "end_ms": study.end_ms,
                        "next_due_ms": study.next_due_ms,
                        "last_observed_ms": study.last_observed_ms,
                        "heartbeat_ms": study.heartbeat_ms,
                        "last_error": study.last_error,
                        "chain_head": study.chain_head,
                        "frozen": "sources" in study.evidence,
                        "exclusion_count": len(study.evidence.get("exclusion", [])),
                        "final_report_ready": study.status == "ended",
                        "positions": [
                            {
                                "base": row.base,
                                "phase": row.phase,
                                "cash": str(row.cash),
                                "fees": str(row.fees),
                                "reserved_risk": str(row.reserved_risk),
                            }
                            for row in positions
                        ],
                        "observation_count": len(observations),
                        "gap_count": sum(
                            event.kind == "observation_gap" for event in events
                        ),
                        "error_count": sum("error" in event.kind for event in events),
                    }
                )
        return {"studies": result}

    async def export(self, database, run_id: str, *, environment: str, account_uid: str) -> bytes:
        """Validate and export one authenticated run as a reproducible ZIP."""
        if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
            raise ValueError("非法运行编号")
        async with database.sessionmaker() as session:
            study = await session.scalar(
                select(ValidationStudy).where(
                    ValidationStudy.environment == environment,
                    ValidationStudy.account_uid == account_uid,
                    ValidationStudy.run_id == run_id,
                )
            )
            if study is None:
                raise ValueError("验证运行不存在")
            positions = list(
                await session.scalars(
                    select(ValidationPosition).where(ValidationPosition.study_id == study.id)
                )
            )
            observations = list(
                await session.scalars(
                    select(ValidationObservation).where(ValidationObservation.study_id == study.id)
                )
            )
            events = list(
                await session.scalars(
                    select(ValidationLedger)
                    .where(ValidationLedger.study_id == study.id)
                    .order_by(ValidationLedger.sequence)
                )
            )
            await verify(session, study)
            data = (
                {column.name: getattr(study, column.name) for column in study.__table__.columns},
                [
                    {column.name: getattr(row, column.name) for column in row.__table__.columns}
                    for row in positions
                ],
                [
                    {column.name: getattr(row, column.name) for column in row.__table__.columns}
                    for row in observations
                ],
                [
                    {column.name: getattr(row, column.name) for column in row.__table__.columns}
                    for row in events
                ],
            )
        return archive_bytes(*data)
