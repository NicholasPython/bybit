"""Durable spread execution facts and frozen pair context.

Revision ID: a612e47bc903
Revises: 3f7c1d92ab40
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "a612e47bc903"
down_revision = "3f7c1d92ab40"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "spread_pair_trade",
        sa.Column("context", postgresql.JSONB(), nullable=False, server_default="{}"),
    )
    op.add_column(
        "spread_pair_trade",
        sa.Column("account_uid", sa.String(64), nullable=False, server_default=""),
    )
    op.alter_column("spread_pair_trade", "context", server_default=None)
    op.alter_column("spread_pair_trade", "account_uid", server_default=None)
    op.create_table(
        "spread_execution",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("environment", sa.String(32), nullable=False),
        sa.Column(
            "order_id", sa.BigInteger(), sa.ForeignKey("spread_leg_order.id"), nullable=False
        ),
        sa.Column("exec_id", sa.String(128), nullable=False),
        sa.Column("exec_qty", sa.Numeric(38, 18), nullable=False),
        sa.Column("exec_price", sa.Numeric(38, 18), nullable=False),
        sa.Column("exec_fee", sa.Numeric(38, 18), nullable=False),
        sa.Column("exec_fee_asset", sa.String(16), nullable=False),
        sa.Column("exec_time", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("environment", "exec_id", name="uq_spread_execution"),
        sa.CheckConstraint("exec_qty > 0 AND exec_price > 0", name="ck_spread_execution_qty"),
    )
    op.create_index("ix_spread_execution_order", "spread_execution", ["order_id"])


def downgrade():
    op.drop_index("ix_spread_execution_order", table_name="spread_execution")
    op.drop_table("spread_execution")
    op.drop_column("spread_pair_trade", "account_uid")
    op.drop_column("spread_pair_trade", "context")
