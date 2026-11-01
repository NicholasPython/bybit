"""记录聚合库存持有周期的实际买入起点。

Revision ID: 8a4d17c2f905
Revises: 786d42c1b808
"""

import sqlalchemy as sa

from alembic import op

revision = "8a4d17c2f905"
down_revision = "786d42c1b808"
branch_labels = None
depends_on = None


def upgrade():
    # 历史库存不猜测起点；缺失时退出规则保持待核实。
    op.add_column("inventory", sa.Column("episode_started_at", sa.DateTime(timezone=True)))
    op.add_column("inventory", sa.Column("episode_intent_id", sa.BigInteger()))
    op.create_foreign_key(
        "fk_inventory_episode_intent", "inventory", "trade_intent",
        ["episode_intent_id"], ["id"], ondelete="SET NULL",
    )


def downgrade():
    op.drop_constraint("fk_inventory_episode_intent", "inventory", type_="foreignkey")
    op.drop_column("inventory", "episode_intent_id")
    op.drop_column("inventory", "episode_started_at")
