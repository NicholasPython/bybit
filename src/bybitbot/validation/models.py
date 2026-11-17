"""Persistent validation runs, positions, observations and append-only events."""

from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from bybitbot.db.models import Base


class ValidationStudy(Base):
    __tablename__ = "validation_study"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    environment: Mapped[str] = mapped_column(String(32), nullable=False)
    account_uid: Mapped[str] = mapped_column(String(64), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    policy: Mapped[str] = mapped_column(String(32), nullable=False)
    direction: Mapped[str] = mapped_column(String(8), nullable=False)
    protocol: Mapped[dict] = mapped_column(JSONB, nullable=False)
    bases: Mapped[list] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running")
    started_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    end_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    next_due_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    last_observed_ms: Mapped[int | None] = mapped_column(BigInteger)
    heartbeat_ms: Mapped[int | None] = mapped_column(BigInteger)
    last_error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    evidence: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    chain_head: Mapped[str] = mapped_column(
        String(64), nullable=False, default="0" * 64, server_default="0" * 64
    )

    __table_args__ = (
        UniqueConstraint("environment", "account_uid", "run_id", name="uq_validation_run"),
        CheckConstraint("environment = 'paper'", name="ck_validation_paper"),
        CheckConstraint("end_ms > started_ms", name="ck_validation_window"),
        Index("ix_validation_due", "environment", "account_uid", "status", "next_due_ms"),
    )


class ValidationPosition(Base):
    __tablename__ = "validation_position"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    study_id: Mapped[int] = mapped_column(ForeignKey("validation_study.id"), nullable=False)
    base: Mapped[str] = mapped_column(String(16), nullable=False)
    phase: Mapped[str] = mapped_column(String(32), nullable=False, default="waiting")
    state: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    cash: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False, default=Decimal(0))
    fees: Mapped[Decimal] = mapped_column(Numeric(38, 18), nullable=False, default=Decimal(0))
    reserved_risk: Mapped[Decimal] = mapped_column(
        Numeric(38, 18), nullable=False, default=Decimal(0)
    )

    __table_args__ = (
        UniqueConstraint("study_id", "base", name="uq_validation_base"),
        CheckConstraint("fees >= 0 AND reserved_risk >= 0", name="ck_validation_amounts"),
    )


class ValidationObservation(Base):
    __tablename__ = "validation_observation"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    study_id: Mapped[int] = mapped_column(ForeignKey("validation_study.id"), nullable=False)
    slot_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    observed_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)

    __table_args__ = (
        UniqueConstraint("study_id", "slot_ms", name="uq_validation_slot"),
        Index("ix_validation_observation", "study_id", "observed_ms"),
    )


class ValidationLedger(Base):
    __tablename__ = "validation_ledger"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    study_id: Mapped[int] = mapped_column(ForeignKey("validation_study.id"), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(128), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    occurred_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)

    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    previous_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    event_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        UniqueConstraint("study_id", "dedupe_key", name="uq_validation_event"),
        UniqueConstraint("study_id", "sequence", name="uq_validation_sequence"),
    )
