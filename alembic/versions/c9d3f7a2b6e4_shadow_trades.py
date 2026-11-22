"""Persist FR-07.3 shadow trading simulation results.

Revision ID: c9d3f7a2b6e4
Revises: b7c9e1f4a2d8
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "c9d3f7a2b6e4"
down_revision = "b7c9e1f4a2d8"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "shadow_trade",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("environment", sa.String(32), nullable=False),
        sa.Column("pair_trade_id", sa.String(64), nullable=False),
        sa.Column("pair_key", sa.String(64), nullable=False),
        sa.Column("decision_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("reason", sa.String(64), nullable=False),
        sa.Column("direction", sa.String(32), nullable=False),
        sa.Column("symbol_a", sa.String(64), nullable=False),
        sa.Column("symbol_b", sa.String(64), nullable=False),
        sa.Column("side_a", sa.String(8), nullable=False),
        sa.Column("side_b", sa.String(8), nullable=False, server_default=""),
        sa.Column("qty_a", sa.Numeric(38, 18), nullable=False, server_default="0"),
        sa.Column("qty_b", sa.Numeric(38, 18), nullable=False, server_default="0"),
        sa.Column("price_a", sa.Numeric(38, 18), nullable=False, server_default="0"),
        sa.Column("price_b", sa.Numeric(38, 18), nullable=False, server_default="0"),
        sa.Column("notional", sa.Numeric(38, 18), nullable=False, server_default="0"),
        sa.Column("z", sa.Numeric(38, 18), nullable=False, server_default="0"),
        sa.Column("spread_pnl", sa.Numeric(38, 18), nullable=False, server_default="0"),
        sa.Column("fee_cost", sa.Numeric(38, 18), nullable=False, server_default="0"),
        sa.Column("slippage_cost", sa.Numeric(38, 18), nullable=False, server_default="0"),
        sa.Column("funding_pnl", sa.Numeric(38, 18), nullable=False, server_default="0"),
        sa.Column("net_pnl", sa.Numeric(38, 18), nullable=False, server_default="0"),
        sa.Column("cumulative_pnl", sa.Numeric(38, 18), nullable=False, server_default="0"),
        sa.Column("depth_ok", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("context", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("environment", "pair_trade_id", name="uq_shadow_trade"),
        sa.CheckConstraint("environment = 'shadow'", name="ck_shadow_environment"),
        sa.CheckConstraint("action in ('OPEN', 'CLOSE', 'STOP', 'NONE')", name="ck_shadow_action"),
        sa.CheckConstraint("decision_at_ms >= 0", name="ck_shadow_time"),
        sa.CheckConstraint("qty_a >= 0 AND qty_b >= 0 AND notional >= 0", name="ck_shadow_qty"),
    )
    op.create_index("ix_shadow_trade_time", "shadow_trade", ["environment", "decision_at_ms"])
    op.create_index(
        "ix_shadow_trade_pair", "shadow_trade", ["environment", "pair_key", "decision_at_ms"]
    )


def downgrade():
    op.drop_index("ix_shadow_trade_pair", table_name="shadow_trade")
    op.drop_index("ix_shadow_trade_time", table_name="shadow_trade")
    op.drop_table("shadow_trade")
