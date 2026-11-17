"""阶段四：运行生命周期、冻结、互斥、终态与证据导出。"""

import io
import json
import zipfile
from decimal import Decimal as D

import pytest
from sqlalchemy import select

from bybitbot.validation import SpreadProtocol
from bybitbot.validation.lifecycle import ValidationLifecycle
from bybitbot.validation.models import ValidationObservation, ValidationPosition, ValidationStudy
from bybitbot.validation.sampling import ValidationStore


async def new_study(sessionmaker, *, run_id="lifecycle", direction="Call", policy="strong_up"):
    store = ValidationStore(sessionmaker, "paper", "account")
    protocol = SpreadProtocol()
    study = await store.start(
        run_id=run_id,
        bases=["ETH"],
        policy=policy,
        direction=direction,
        protocol=protocol,
        started_ms=1000,
    )
    return store, ValidationLifecycle(store), study


async def test_freeze_checkpoint_heartbeat_status_and_final_export(database, sessionmaker):
    store, life, study = await new_study(sessionmaker)
    async with sessionmaker() as session:
        position = await session.scalar(select(ValidationPosition))
        position.state = {
            "long_qty": "0",
            "short_qty": "0",
            "fees_known": True,
        }
        session.add(
            ValidationObservation(
                study_id=study.id,
                slot_ms=1000,
                observed_ms=1001,
                status="ok",
                snapshot_hash="0" * 64,
                payload={"bases": {"ETH": {}}},
            )
        )
        await session.commit()
    # 观察哈希先修正为真实值，否则导出应拒绝损坏证据。
    async with sessionmaker() as session:
        observation = await session.scalar(select(ValidationObservation))
        observation.snapshot_hash = json_value_hash(observation.payload)
        await session.commit()

    evidence = await life.freeze(study.id)
    assert "sources" in evidence and "protocol_hash" in evidence
    await life.heartbeat(study.id, status="ending")
    await life.checkpoint(study.id)
    await life.check(study.id)
    status = await life.status(database, environment="paper", account_uid="account")
    row = status["studies"][0]
    assert row["run_id"] == "lifecycle"
    assert row["status"] == "ending"
    assert row["frozen"] is True
    assert row["final_report_ready"] is False
    refrozen = await life.freeze(study.id)
    assert all(refrozen.get(k) == v for k, v in evidence.items())

    result = await life.end(study.id)
    assert result["status"] == "ended"
    assert result["unresolved_bases"] == []
    data = await life.export(database, "lifecycle", environment="paper", account_uid="account")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = set(archive.namelist())
        required = {"manifest.json", "state.json", "ledger.jsonl", "STATUS.md", "FINAL_REPORT.md"}
        assert required <= names
        report = archive.read("FINAL_REPORT.md").decode()
        assert "lifecycle" in report
        heartbeat = json.loads(archive.read("heartbeat.json"))
        assert heartbeat["status"] == "ended"
        events = [json.loads(line) for line in archive.read("ledger.jsonl").splitlines()]
        assert events[-1]["kind"] == "checkpoint"

    other = await life.status(database, environment="paper", account_uid="other")
    assert other["studies"] == []
    with pytest.raises(ValueError, match="不存在"):
        await life.export(database, "lifecycle", environment="paper", account_uid="other")


def json_value_hash(value):
    from bybitbot.validation.sampling import digest

    return digest(value)


async def test_end_preserves_unresolved_exposure(sessionmaker):
    _, life, study = await new_study(sessionmaker, run_id="unresolved")
    async with sessionmaker() as session:
        position = await session.scalar(select(ValidationPosition))
        position.state = {
            "long_qty": "0.4",
            "short_qty": "0.1",
            "reserved_risk": "0",
            "fees_known": True,
        }
        await session.commit()
    await life.freeze(study.id)
    await life.checkpoint(study.id)
    result = await life.end(study.id)
    assert result["unresolved_bases"] == ["ETH"]
    async with sessionmaker() as session:
        position = await session.scalar(select(ValidationPosition))
        assert position.phase == "study_ended_unresolved"
        assert position.state["unresolved_risk"] is True
        assert D(position.state["long_qty"]) == D("0.4")


async def test_exclusion_fail_closed_on_peer_change(sessionmaker, monkeypatch):
    store, life, study = await new_study(sessionmaker, run_id="bull")
    store_bear = ValidationStore(sessionmaker, "paper", "account")
    peer = await store_bear.start(
        run_id="bear",
        bases=["ETH"],
        policy="weak_down",
        direction="Put",
        protocol=SpreadProtocol(target_entry_days=22, study_days=6),
        started_ms=1000,
    )
    await life.freeze(study.id)
    proof = await life.register_exclusion(study.id, other_run_ids=["bear"])
    assert proof["opposing"][0]["run_id"] == "bear"
    await life.checkpoint(study.id)
    await life.check(study.id)

    async with sessionmaker() as session:
        row = await session.get(ValidationStudy, peer.id)
        row.chain_head = "f" * 64
        await session.commit()
    with pytest.raises(ValueError, match="互斥运行状态"):
        await life.check(study.id)

    with pytest.raises(ValueError, match="对向"):
        await life.register_exclusion(study.id, other_run_ids=["bull"])


async def test_source_change_fails_check(sessionmaker, monkeypatch):
    import bybitbot.validation.lifecycle as lifecycle

    _, life, study = await new_study(sessionmaker, run_id="source-check")
    await life.freeze(study.id)
    await life.checkpoint(study.id)

    def broken(*args, **kwargs):
        raise ValueError("product source changed: src/bybitbot/service.py")

    monkeypatch.setattr(lifecycle, "verify_sources", broken)
    with pytest.raises(ValueError, match="product source changed"):
        await life.check(study.id)


async def test_freeze_multiple_bindings_is_deterministic(sessionmaker):
    store, life, study = await new_study(sessionmaker, run_id="multi")
    async with sessionmaker() as session:
        for row in await session.scalars(select(ValidationPosition)):
            row.state = {"binding": {"strategy_id": 1, "version": 1}}
        await session.commit()
    first = await life.freeze(study.id)
    second = await life.freeze(study.id)
    assert first["bindings"] == second["bindings"] == [{"strategy_id": 1, "version": 1}]
