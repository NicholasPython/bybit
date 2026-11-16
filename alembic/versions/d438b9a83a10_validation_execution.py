"""Canonical paper spread intent scope, directional inventory and ledger chain.

Revision ID: d438b9a83a10
Revises: c314a70d2b91
"""

import hashlib
import json

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "d438b9a83a10"
down_revision = "c314a70d2b91"
branch_labels = None
depends_on = None

GENESIS = "0" * 64


def _canonical(value) -> str:
    return json.dumps(
        value,
        default=lambda obj: obj.isoformat() if hasattr(obj, "isoformat") else str(obj),
        sort_keys=True,
        allow_nan=False,
    )


def _digest(value) -> str:
    return hashlib.sha256(
        json.dumps(
            _canonical(value), sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def _event_hash(previous: str, event: dict) -> str:
    return _digest({"previous_hash": previous, **event})


def upgrade():
    op.add_column(
        "trade_intent", sa.Column("validation_position_id", sa.BigInteger(), nullable=True)
    )
    op.add_column("trade_intent", sa.Column("coverage_symbol", sa.String(64), nullable=True))
    op.create_foreign_key(
        "fk_intent_validation_position",
        "trade_intent",
        "validation_position",
        ["validation_position_id"],
        ["id"],
    )
    op.add_column(
        "inventory",
        sa.Column("position_side", sa.String(8), server_default="Buy", nullable=False),
    )
    op.drop_constraint("uq_inventory", "inventory", type_="unique")
    op.create_unique_constraint(
        "uq_inventory", "inventory", ["environment", "strategy_id", "symbol", "position_side"]
    )
    op.create_check_constraint(
        "ck_inventory_side", "inventory", "position_side IN ('Buy', 'Sell')"
    )
    # 账本链字段必须与运行中的写入同时存在：证据链不能有一段没有哈希的历史。
    op.add_column(
        "validation_study",
        sa.Column("chain_head", sa.String(64), server_default=GENESIS, nullable=False),
    )
    op.add_column(
        "validation_study",
        sa.Column(
            "evidence",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column("validation_ledger", sa.Column("sequence", sa.BigInteger(), nullable=True))
    op.add_column("validation_ledger", sa.Column("previous_hash", sa.String(64), nullable=True))
    op.add_column("validation_ledger", sa.Column("event_hash", sa.String(64), nullable=True))
    _backfill_chain()
    op.alter_column("validation_ledger", "sequence", nullable=False)
    op.alter_column("validation_ledger", "previous_hash", nullable=False)
    op.alter_column("validation_ledger", "event_hash", nullable=False)
    op.create_unique_constraint(
        "uq_validation_sequence", "validation_ledger", ["study_id", "sequence"]
    )


def _backfill_chain() -> None:
    """已有事件按写入顺序补齐链：同一运行内序号连续、哈希首尾相接。"""
    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            "SELECT id, study_id, dedupe_key, kind, occurred_ms, payload "
            "FROM validation_ledger ORDER BY study_id, id"
        )
    ).all()
    sequences: dict[int, int] = {}
    heads: dict[int, str] = {}
    for row in rows:
        sequence = sequences.get(row.study_id, 0) + 1
        parent = heads.get(row.study_id, GENESIS)
        body = {
            "sequence": sequence,
            "kind": row.kind,
            "dedupe_key": row.dedupe_key,
            "occurred_ms": int(row.occurred_ms),
            "payload": row.payload,
        }
        digest = _event_hash(parent, body)
        connection.execute(
            sa.text(
                "UPDATE validation_ledger SET sequence = :sequence, previous_hash = :previous, "
                "event_hash = :event_hash WHERE id = :id"
            ),
            {
                "id": row.id,
                "sequence": sequence,
                "previous": parent,
                "event_hash": digest,
            },
        )
        sequences[row.study_id] = sequence
        heads[row.study_id] = digest
    for study_id, digest in heads.items():
        connection.execute(
            sa.text("UPDATE validation_study SET chain_head = :chain_head WHERE id = :id"),
            {"id": study_id, "chain_head": digest},
        )


def downgrade():
    # Downgrading cannot silently reinterpret live shorts as long inventory.
    connection = op.get_bind()
    if connection.scalar(
        sa.text("SELECT count(*) FROM inventory WHERE position_side = 'Sell' AND qty > 0")
    ):
        raise RuntimeError("纸面空头尚未平仓，禁止移除方向字段")
    op.drop_constraint("uq_validation_sequence", "validation_ledger", type_="unique")
    op.drop_column("validation_ledger", "event_hash")
    op.drop_column("validation_ledger", "previous_hash")
    op.drop_column("validation_ledger", "sequence")
    op.drop_column("validation_study", "evidence")
    op.drop_column("validation_study", "chain_head")
    op.drop_constraint("ck_inventory_side", "inventory", type_="check")
    op.drop_constraint("uq_inventory", "inventory", type_="unique")
    op.create_unique_constraint(
        "uq_inventory", "inventory", ["environment", "strategy_id", "symbol"]
    )
    op.drop_column("inventory", "position_side")
    op.drop_constraint("fk_intent_validation_position", "trade_intent", type_="foreignkey")
    op.drop_column("trade_intent", "coverage_symbol")
    op.drop_column("trade_intent", "validation_position_id")
