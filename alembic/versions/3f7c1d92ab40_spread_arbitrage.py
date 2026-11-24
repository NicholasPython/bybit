"""Spread arbitrage pairs, pair trades, leg orders and reconciliation logs.

Revision ID: 3f7c1d92ab40
Revises: d438b9a83a10
"""

import sqlalchemy as sa

from alembic import op

revision = "3f7c1d92ab40"
down_revision = "d438b9a83a10"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "spread_pair_config",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("environment", sa.String(32), nullable=False),
        sa.Column("pair_key", sa.String(64), nullable=False),
        sa.Column("symbol_a", sa.String(64), nullable=False),
        sa.Column("symbol_b", sa.String(64), nullable=False),
        sa.Column("hedge_ratio", sa.Numeric(38, 18), nullable=False),
        sa.Column("z_entry", sa.Numeric(18, 8), nullable=False),
        sa.Column("z_exit", sa.Numeric(18, 8), nullable=False),
        sa.Column("z_stop", sa.Numeric(18, 8), nullable=False),
        sa.Column("max_notional", sa.Numeric(38, 18), nullable=False),
        sa.Column("max_unhedged_notional", sa.Numeric(38, 18), nullable=False),
        sa.Column("passive_timeout_ms", sa.Integer(), nullable=False),
        sa.Column("recovery_window_ms", sa.Integer(), nullable=False),
        sa.Column("max_slippage_bps", sa.Numeric(18, 4), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("environment", "pair_key", name="uq_spread_pair_config"),
        sa.CheckConstraint(
            "status in ('ACTIVE', 'PAUSED', 'DISABLED')", name="ck_spread_pair_config_status"
        ),
        sa.CheckConstraint("hedge_ratio > 0", name="ck_spread_pair_config_ratio"),
        sa.CheckConstraint("z_exit < z_entry AND z_entry < z_stop", name="ck_spread_pair_config_z"),
        sa.CheckConstraint("max_notional > 0", name="ck_spread_pair_config_notional"),
        sa.CheckConstraint(
            "max_unhedged_notional >= 0 AND max_unhedged_notional < max_notional",
            name="ck_spread_pair_config_unhedged",
        ),
    )
    op.create_index("ix_spread_pair_config_status", "spread_pair_config", ["environment", "status"])

    op.create_table(
        "spread_pair_trade",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("environment", sa.String(32), nullable=False),
        sa.Column("pair_trade_id", sa.String(32), nullable=False),
        sa.Column("pair_key", sa.String(64), nullable=False),
        sa.Column(
            "config_id",
            sa.BigInteger(),
            sa.ForeignKey("spread_pair_config.id", ondelete="SET NULL"),
        ),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("direction", sa.String(16), nullable=False),
        sa.Column("symbol_a", sa.String(64), nullable=False),
        sa.Column("symbol_b", sa.String(64), nullable=False),
        sa.Column("side_a", sa.String(8), nullable=False),
        sa.Column("side_b", sa.String(8), nullable=False),
        sa.Column("target_notional", sa.Numeric(38, 18), nullable=False),
        sa.Column("filled_qty_a", sa.Numeric(38, 18), nullable=False),
        sa.Column("filled_qty_b", sa.Numeric(38, 18), nullable=False),
        sa.Column("avg_price_a", sa.Numeric(38, 18)),
        sa.Column("avg_price_b", sa.Numeric(38, 18)),
        sa.Column("notional_a", sa.Numeric(38, 18), nullable=False),
        sa.Column("notional_b", sa.Numeric(38, 18), nullable=False),
        sa.Column("expected_profit", sa.Numeric(38, 18), nullable=False),
        sa.Column("spread_pnl", sa.Numeric(38, 18), nullable=False),
        sa.Column("funding_pnl", sa.Numeric(38, 18), nullable=False),
        sa.Column("trading_fee", sa.Numeric(38, 18), nullable=False),
        sa.Column("net_pnl", sa.Numeric(38, 18), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("hedged_at", sa.DateTime(timezone=True)),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("environment", "pair_trade_id", name="uq_spread_pair_trade"),
        sa.CheckConstraint(
            "state in ('OPENING', 'HEDGED', 'CLOSING', 'CLOSED', 'ABORTED', 'ERROR_UNHEDGED')",
            name="ck_spread_pair_trade_state",
        ),
        sa.CheckConstraint(
            "direction in ('SHORT_A_LONG_B', 'LONG_A_SHORT_B')",
            name="ck_spread_pair_trade_direction",
        ),
        sa.CheckConstraint(
            "filled_qty_a >= 0 AND filled_qty_b >= 0", name="ck_spread_pair_trade_qty"
        ),
        sa.CheckConstraint("target_notional > 0", name="ck_spread_pair_trade_notional"),
    )
    op.create_index("ix_spread_pair_trade_state", "spread_pair_trade", ["environment", "state"])
    op.create_index(
        "ix_spread_pair_trade_pair_key",
        "spread_pair_trade",
        ["environment", "pair_key", "created_at"],
    )

    op.create_table(
        "spread_leg_order",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("environment", sa.String(32), nullable=False),
        sa.Column(
            "pair_trade_id",
            sa.BigInteger(),
            sa.ForeignKey("spread_pair_trade.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("leg", sa.String(1), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("order_link_id", sa.String(36), nullable=False),
        sa.Column("exchange_order_id", sa.String(64)),
        sa.Column("symbol", sa.String(64), nullable=False),
        sa.Column("side", sa.String(8), nullable=False),
        sa.Column("order_type", sa.String(16), nullable=False),
        sa.Column("time_in_force", sa.String(16), nullable=False),
        sa.Column("price", sa.Numeric(38, 18), nullable=False),
        sa.Column("qty", sa.Numeric(38, 18), nullable=False),
        sa.Column("qty_filled", sa.Numeric(38, 18), nullable=False),
        sa.Column("avg_price", sa.Numeric(38, 18)),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("sync_state", sa.String(16), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("environment", "order_link_id", name="uq_spread_leg_order_link"),
        sa.CheckConstraint("leg in ('A', 'B')", name="ck_spread_leg_order_leg"),
        sa.CheckConstraint(
            "role in ('PASSIVE', 'ACTIVE', 'RECOVERY', 'FLATTEN')",
            name="ck_spread_leg_order_role",
        ),
        sa.CheckConstraint(
            "qty > 0 AND qty_filled >= 0 AND qty_filled <= qty", name="ck_spread_leg_qty"
        ),
        sa.CheckConstraint(
            "status in ('PENDING', 'WORKING', 'PARTIAL', 'FILLED', "
            "'CANCELED', 'REJECTED', 'UNKNOWN')",
            name="ck_spread_leg_order_status",
        ),
        sa.CheckConstraint(
            "sync_state in ('NONE', 'PENDING', 'CONFIRMED', 'UNCERTAIN')",
            name="ck_spread_leg_order_sync",
        ),
    )
    op.create_index("ix_spread_leg_order_pair", "spread_leg_order", ["pair_trade_id", "leg"])
    op.create_index(
        "ix_spread_leg_order_link", "spread_leg_order", ["environment", "order_link_id"]
    )

    op.create_table(
        "spread_reconciliation_log",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("environment", sa.String(32), nullable=False),
        sa.Column(
            "pair_trade_id",
            sa.BigInteger(),
            sa.ForeignKey("spread_pair_trade.id", ondelete="CASCADE"),
        ),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("local_qty_a", sa.Numeric(38, 18), nullable=False),
        sa.Column("local_qty_b", sa.Numeric(38, 18), nullable=False),
        sa.Column("exchange_qty_a", sa.Numeric(38, 18), nullable=False),
        sa.Column("exchange_qty_b", sa.Numeric(38, 18), nullable=False),
        sa.Column("local_notional_a", sa.Numeric(38, 18), nullable=False),
        sa.Column("local_notional_b", sa.Numeric(38, 18), nullable=False),
        sa.Column("exchange_notional_a", sa.Numeric(38, 18), nullable=False),
        sa.Column("exchange_notional_b", sa.Numeric(38, 18), nullable=False),
        sa.Column("difference", sa.Numeric(38, 18), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("actions", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status in ('CLEAN', 'DIFFERENCE', 'REPAIRED')",
            name="ck_spread_reconciliation_status",
        ),
        sa.CheckConstraint("difference >= 0", name="ck_spread_reconciliation_difference"),
    )
    op.create_index(
        "ix_spread_reconciliation_checked",
        "spread_reconciliation_log",
        ["environment", "checked_at"],
    )
    op.create_index(
        "ix_spread_reconciliation_pair",
        "spread_reconciliation_log",
        ["pair_trade_id", "checked_at"],
    )


def downgrade():
    op.drop_index("ix_spread_reconciliation_pair", table_name="spread_reconciliation_log")
    op.drop_index("ix_spread_reconciliation_checked", table_name="spread_reconciliation_log")
    op.drop_table("spread_reconciliation_log")
    op.drop_index("ix_spread_leg_order_link", table_name="spread_leg_order")
    op.drop_index("ix_spread_leg_order_pair", table_name="spread_leg_order")
    op.drop_table("spread_leg_order")
    op.drop_index("ix_spread_pair_trade_pair_key", table_name="spread_pair_trade")
    op.drop_index("ix_spread_pair_trade_state", table_name="spread_pair_trade")
    op.drop_table("spread_pair_trade")
    op.drop_index("ix_spread_pair_config_status", table_name="spread_pair_config")
    op.drop_table("spread_pair_config")
