"""Persist rolling spread signal samples.

Revision ID: b7c9e1f4a2d8
Revises: a612e47bc903
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "b7c9e1f4a2d8"
down_revision = "a612e47bc903"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "spread_signal_sample",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("environment", sa.String(32), nullable=False),
        sa.Column("pair_key", sa.String(64), nullable=False),
        sa.Column("at_ms", sa.BigInteger(), nullable=False),
        sa.Column("price_a", sa.Numeric(38, 18), nullable=False),
        sa.Column("price_b", sa.Numeric(38, 18), nullable=False),
        sa.Column("funding_a", sa.Numeric(38, 18), nullable=False, server_default="0"),
        sa.Column("funding_b", sa.Numeric(38, 18), nullable=False, server_default="0"),
        sa.Column("raw", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("environment", "pair_key", "at_ms", name="uq_spread_signal_sample"),
        sa.CheckConstraint("price_a > 0 AND price_b > 0", name="ck_spread_signal_price"),
        sa.CheckConstraint("at_ms >= 0", name="ck_spread_signal_time"),
    )
    op.create_index(
        "ix_spread_signal_window", "spread_signal_sample", ["environment", "pair_key", "at_ms"]
    )


def downgrade():
    op.drop_index("ix_spread_signal_window", table_name="spread_signal_sample")
    op.drop_table("spread_signal_sample")
