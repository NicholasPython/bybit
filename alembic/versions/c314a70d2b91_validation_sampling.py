"""Persistent validation sampling and cohort reservations.

Revision ID: c314a70d2b91
Revises: 8a4d17c2f905
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "c314a70d2b91"
down_revision = "8a4d17c2f905"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "validation_study",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("environment", sa.String(32), nullable=False),
        sa.Column("account_uid", sa.String(64), nullable=False),
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.Column("policy", sa.String(32), nullable=False),
        sa.Column("direction", sa.String(8), nullable=False),
        sa.Column("protocol", postgresql.JSONB(), nullable=False),
        sa.Column("bases", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("started_ms", sa.BigInteger(), nullable=False),
        sa.Column("end_ms", sa.BigInteger(), nullable=False),
        sa.Column("next_due_ms", sa.BigInteger(), nullable=False),
        sa.Column("last_observed_ms", sa.BigInteger()),
        sa.Column("heartbeat_ms", sa.BigInteger()),
        sa.Column("last_error", sa.Text(), nullable=False),
        sa.UniqueConstraint("environment", "account_uid", "run_id", name="uq_validation_run"),
        sa.CheckConstraint("environment = 'paper'", name="ck_validation_paper"),
        sa.CheckConstraint("end_ms > started_ms", name="ck_validation_window"),
    )
    op.create_index(
        "ix_validation_due",
        "validation_study",
        ["environment", "account_uid", "status", "next_due_ms"],
    )
    op.create_table(
        "validation_position",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "study_id", sa.BigInteger(), sa.ForeignKey("validation_study.id"), nullable=False
        ),
        sa.Column("base", sa.String(16), nullable=False),
        sa.Column("phase", sa.String(32), nullable=False),
        sa.Column("state", postgresql.JSONB(), nullable=False),
        sa.Column("cash", sa.Numeric(38, 18), nullable=False),
        sa.Column("fees", sa.Numeric(38, 18), nullable=False),
        sa.Column("reserved_risk", sa.Numeric(38, 18), nullable=False),
        sa.UniqueConstraint("study_id", "base", name="uq_validation_base"),
        sa.CheckConstraint("fees >= 0 AND reserved_risk >= 0", name="ck_validation_amounts"),
    )
    op.create_table(
        "validation_observation",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "study_id", sa.BigInteger(), sa.ForeignKey("validation_study.id"), nullable=False
        ),
        sa.Column("slot_ms", sa.BigInteger(), nullable=False),
        sa.Column("observed_ms", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("snapshot_hash", sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.UniqueConstraint("study_id", "slot_ms", name="uq_validation_slot"),
    )
    op.create_index(
        "ix_validation_observation", "validation_observation", ["study_id", "observed_ms"]
    )
    op.create_table(
        "validation_ledger",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "study_id", sa.BigInteger(), sa.ForeignKey("validation_study.id"), nullable=False
        ),
        sa.Column("dedupe_key", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("occurred_ms", sa.BigInteger(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.UniqueConstraint("study_id", "dedupe_key", name="uq_validation_event"),
    )


def downgrade():
    op.drop_table("validation_ledger")
    op.drop_index("ix_validation_observation", table_name="validation_observation")
    op.drop_table("validation_observation")
    op.drop_table("validation_position")
    op.drop_index("ix_validation_due", table_name="validation_study")
    op.drop_table("validation_study")
