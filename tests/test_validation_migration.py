"""Upgrade/check/downgrade in a newly owned schema, without touching existing ledgers."""

import asyncio
import os
import uuid

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool


def migration_cycle():
    from alembic.config import Config

    from alembic import command

    dsn = os.environ.get(
        "BYBITBOT_TEST_DSN", "postgresql+psycopg://postgres:bybitbot@127.0.0.1:55432/bybitbot"
    )
    schema = "test_migration_" + uuid.uuid4().hex
    admin = create_engine(dsn, hide_parameters=True, poolclass=NullPool)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    try:
        scoped = make_url(dsn).update_query_dict({"options": f"-csearch_path={schema}"})
        config = Config("alembic.ini")
        config.attributes["db_dsn"] = scoped.render_as_string(hide_password=False)
        command.upgrade(config, "head")
        command.check(config)
        command.downgrade(config, "8a4d17c2f905")
        command.upgrade(config, "head")
        command.check(config)
    finally:
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


async def test_validation_migration_roundtrip_converges():
    await asyncio.to_thread(migration_cycle)
