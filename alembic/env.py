"""Alembic 环境。

DSN 从环境变量 ``BYBITBOT_DB_DSN`` 读取 —— **不写进 ``alembic.ini``**，
避免把连接串（可能含密码）提交进 Git（SEC-01）。
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context
from bybitbot.db.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _dsn() -> str:
    dsn = os.environ.get("BYBITBOT_DB_DSN")
    if not dsn:
        raise RuntimeError(
            "请设置 BYBITBOT_DB_DSN 后再运行迁移，例如：\n"
            "  export BYBITBOT_DB_DSN='postgresql+psycopg://bybitbot:***@127.0.0.1:55432/bybitbot'"
        )
    return dsn


def run_migrations_offline() -> None:
    context.configure(
        url=_dsn(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = _dsn()
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
