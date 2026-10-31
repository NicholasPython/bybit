"""修正检查点不可空约束。

Revision ID: 786d42c1b808
Revises: 6bb1d24a9e42
"""

from alembic import op

revision = "786d42c1b808"
down_revision = "6bb1d24a9e42"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column("runtime_state", "value", nullable=False)


def downgrade():
    op.alter_column("runtime_state", "value", nullable=True)
