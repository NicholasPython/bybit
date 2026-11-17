"""Append-only validation evidence, serialized under the study row lock."""

from __future__ import annotations

from sqlalchemy import select

from bybitbot.validation.archive import checkpoint_state, event_hash, verify_chain
from bybitbot.validation.models import ValidationLedger, ValidationObservation, ValidationPosition

GENESIS = "0" * 64


def row_value(row):
    from bybitbot.validation.sampling import json_value

    return json_value({column.name: getattr(row, column.name) for column in row.__table__.columns})


async def contents(session, study):
    positions = list(await session.scalars(
        select(ValidationPosition).where(ValidationPosition.study_id == study.id)
        .order_by(ValidationPosition.id)
    ))
    observations = list(await session.scalars(
        select(ValidationObservation).where(ValidationObservation.study_id == study.id)
        .order_by(ValidationObservation.id)
    ))
    events = list(await session.scalars(
        select(ValidationLedger).where(ValidationLedger.study_id == study.id)
        .order_by(ValidationLedger.sequence)
    ))
    return (
        row_value(study),
        [row_value(p) for p in positions],
        [row_value(o) for o in observations],
        [row_value(e) for e in events],
    )


async def verify(session, study):
    from bybitbot.validation.sampling import digest

    data, positions, observations, events = await contents(session, study)
    verify_chain(events, study.chain_head)
    for observation in observations:
        if digest(observation["payload"]) != observation["snapshot_hash"]:
            raise ValueError("验证观察哈希不符")
    saved = study.evidence.get("checkpoint_hash")
    if saved and saved != digest(checkpoint_state(data, positions, observations)):
        raise ValueError("验证状态检查点不符")
    if saved and (
        not events
        or events[-1]["kind"] != "checkpoint"
        or events[-1]["payload"].get("checkpoint_hash") != saved
    ):
        raise ValueError("验证检查点未锚定到账本")


async def append_event(session, study, *, dedupe_key, kind, occurred_ms, payload):
    from bybitbot.validation.sampling import json_value

    await session.flush()
    existing = await session.scalar(select(ValidationLedger).where(
        ValidationLedger.study_id == study.id, ValidationLedger.dedupe_key == dedupe_key,
    ))
    body = {
        "dedupe_key": dedupe_key,
        "kind": kind,
        "occurred_ms": occurred_ms,
        "payload": json_value(payload),
    }
    if existing:
        if any(getattr(existing, key) != value for key, value in body.items()):
            raise ValueError("验证事件编号已绑定其他内容")
        return existing
    last = await session.scalar(select(ValidationLedger).where(
        ValidationLedger.study_id == study.id,
    ).order_by(ValidationLedger.sequence.desc()).limit(1))
    sequence = last.sequence + 1 if last else 1
    previous = last.event_hash if last else GENESIS
    if previous != study.chain_head:
        raise ValueError("验证账本头不符")
    body["sequence"] = sequence
    hashed = event_hash(previous, body)
    row = ValidationLedger(study_id=study.id, previous_hash=previous, event_hash=hashed, **body)
    session.add(row)
    study.chain_head = hashed
    await session.flush()
    return row


async def checkpoint(session, study, *, now_ms):
    from bybitbot.validation.sampling import digest

    await session.flush()
    data, positions, observations, _ = await contents(session, study)
    hashed = digest(checkpoint_state(data, positions, observations))
    if study.evidence.get("checkpoint_hash") == hashed:
        return
    study.evidence = study.evidence | {"checkpoint_hash": hashed}
    await append_event(
        session,
        study,
        dedupe_key=f"checkpoint:{study.chain_head}",
        kind="checkpoint",
        occurred_ms=now_ms,
        payload={"checkpoint_hash": hashed},
    )
