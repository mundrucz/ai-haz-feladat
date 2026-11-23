"""Aszinkron SQLite-hozzáférés SQLAlchemy-vel. A séma indításkor az Alembic-migrációkból épül fel."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_IMMEDIATE = "sqlite_begin_immediate"


def _on_connect(dbapi_connection: Any, _record: Any) -> None:
    # A tranzakciót nem a meghajtó indítja, hanem az `_on_begin`: így a migráció (DDL) is egy tranzakcióban fut,
    # és kérhető `BEGIN IMMEDIATE`.
    dbapi_connection.isolation_level = None
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys = ON")
    cursor.execute("PRAGMA busy_timeout = 5000")
    cursor.execute("PRAGMA journal_mode = WAL")
    cursor.close()


def _on_begin(conn: Connection) -> None:
    conn.exec_driver_sql("BEGIN IMMEDIATE" if conn.get_execution_options().get(_IMMEDIATE) else "BEGIN")


def _upgrade_to_head(connection: Connection) -> None:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.attributes["connection"] = connection
    command.upgrade(config, "head")


class Database:
    """Minden munkamenet saját kapcsolatot kap (`NullPool`), így a párhuzamos feldolgozás biztonságos."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{path}", poolclass=NullPool)
        event.listen(self.engine.sync_engine, "connect", _on_connect)
        event.listen(self.engine.sync_engine, "begin", _on_begin)
        self._sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Munkamenet olvasáshoz; a nem véglegesített változások a végén elvesznek."""
        async with self._sessions() as session:
            yield session

    @asynccontextmanager
    async def transaction(self, *, immediate: bool = False) -> AsyncIterator[AsyncSession]:
        """Írási munkamenet: a végén véglegesít, kivételnél visszagörget.

        `immediate=True` esetén az írási zárat már a tranzakció elején megszerzi, így az ellenőrzés és az írás
        között más nem írhat (párhuzamos mentésnél sincs versenyhelyzet).
        """
        async with self._sessions() as session, session.begin():
            await session.connection(execution_options={_IMMEDIATE: immediate})
            yield session

    async def init(self) -> None:
        """A sémát a legutóbbi Alembic-revízióra hozza; a migráció és a verzió rögzítése egy tranzakció."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with self.engine.begin() as conn:
            await conn.run_sync(_upgrade_to_head)

    async def ping(self) -> bool:
        try:
            async with self.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception:
            logger.exception("database ping failed")
            return False

    async def close(self) -> None:
        await self.engine.dispose()
