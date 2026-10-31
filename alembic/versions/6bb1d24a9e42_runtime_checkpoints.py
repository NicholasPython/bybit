"""持久化服务心跳与模拟检查点。

Revision ID: 6bb1d24a9e42
Revises: e66a2b075bb5
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "6bb1d24a9e42"
down_revision = "e66a2b075bb5"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "runtime_state",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("environment", sa.String(32), nullable=False),
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("value", postgresql.JSONB(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("environment", "key", name="uq_runtime_state"),
    )


def downgrade():
    op.drop_table("runtime_state")
