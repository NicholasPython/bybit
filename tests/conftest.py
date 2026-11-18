"""All tests use synthetic credentials, blocked external HTTP and isolated DB schemas."""

from __future__ import annotations

import os
import uuid

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from bybitbot.db.session import Database


@pytest.fixture(autouse=True)
def block_exchange_network(monkeypatch):
    monkeypatch.setenv("BYBITBOT_TESTING", "1")

    async def blocked(*args, **kwargs):
        raise AssertionError("External HTTP is disabled in tests; use MockTransport")

    def blocked_sync(*args, **kwargs):
        raise AssertionError("External HTTP is disabled in tests; use MockTransport")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", blocked)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked_sync)


@pytest_asyncio.fixture
async def database():
    dsn = os.environ.get(
        "BYBITBOT_TEST_DSN",
        "postgresql+psycopg://postgres:bybitbot@127.0.0.1:55432/bybitbot",
    )
    schema = "test_" + uuid.uuid4().hex
    admin = create_async_engine(dsn, hide_parameters=True)
    async with admin.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    separator = "&" if "?" in dsn else "?"
    db = Database(f"{dsn}{separator}options=-csearch_path%3D{schema}")
    try:
        await db.create_all()
        yield db
    finally:
        await db.dispose()
        async with admin.begin() as connection:
            # 任何遗留的未关闭事务都会持有本 schema 的行锁，而
            # DROP SCHEMA ... CASCADE 需要 AccessExclusiveLock —— 不设上限
            # 就会永久等待（CI 上表现为卡满 6 小时被自动取消）。设 lock_timeout
            # 让它快速失败并把真凶指出来，而不是把整轮回归挂死。
            await connection.execute(text("SET lock_timeout = '30s'"))
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


@pytest.fixture
def sessionmaker(database):
    return database.sessionmaker
