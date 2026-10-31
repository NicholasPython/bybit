"""数据库会话与事务管理。

所有写操作必须走 :func:`session_scope` 或显式事务边界；RSK-02 要求资金与
可平仓数量的校验与预留落在**同一个持久化事务**里，因此执行器禁止在事务
中间调用交易所接口。
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from bybitbot.db.models import Base

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


class Database:
    """数据库句柄。"""

    def __init__(self, dsn: str, *, echo: bool = False) -> None:
        self._engine = create_async_engine(
            dsn,
            echo=echo,
            hide_parameters=True,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=5,
            future=True,
        )
        self._sessionmaker = async_sessionmaker(
            self._engine, expire_on_commit=False, class_=AsyncSession
        )

    @property
    def engine(self) -> AsyncEngine:
        assert self._engine is not None
        return self._engine

    @property
    def sessionmaker(self) -> async_sessionmaker[AsyncSession]:
        assert self._sessionmaker is not None
        return self._sessionmaker

    async def create_all(self) -> None:
        """按模型建表。

        生产环境用 Alembic 迁移；这里用于测试与首次引导。
        """
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def drop_all(self) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)

    async def dispose(self) -> None:
        await self._engine.dispose()


_db: Database | None = None


def get_db() -> Database:
    """进程级单例。"""
    global _db
    if _db is None:
        raise RuntimeError("数据库尚未初始化，请先调用 init_db()")
    return _db


def init_db(dsn: str, *, echo: bool = False) -> Database:
    global _db
    _db = Database(dsn, echo=echo)
    return _db


@contextlib.asynccontextmanager
async def session_scope(db: Database | None = None) -> AsyncIterator[AsyncSession]:
    """事务边界。正常结束提交，异常回滚。"""
    target = db or get_db()
    async with target.sessionmaker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def fetch_one(db: Database, stmt: Any) -> Any:
    async with db.sessionmaker() as session:
        return await session.scalar(stmt)


__all__ = ["Base", "Database", "fetch_one", "get_db", "init_db", "session_scope"]
