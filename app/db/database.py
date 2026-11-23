import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

# (verzió, SQL) párok; új séma-változás új, növekvő verzióként kerül ide.
Migration = tuple[int, str]
MIGRATIONS: list[Migration] = [
    (
        1,
        """
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content_hash TEXT NOT NULL UNIQUE,
            original_name TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('pending', 'processing', 'done', 'failed')),
            error_code TEXT,
            error_message TEXT,
            page_count INTEGER,
            text TEXT,
            uploaded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            processed_at TEXT
        );
        CREATE INDEX idx_documents_status ON documents (status);
        """,
    ),
]


class Database:
    """Aszinkron SQLite-hozzáférés: minden művelet saját kapcsolatot kap, így a párhuzamos feldolgozás biztonságos."""

    def __init__(self, path: Path) -> None:
        self.path = path

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[aiosqlite.Connection]:
        conn = await aiosqlite.connect(self.path)
        conn.row_factory = aiosqlite.Row
        try:
            await conn.execute("PRAGMA foreign_keys = ON")
            await conn.execute("PRAGMA busy_timeout = 5000")
            yield conn
        finally:
            await conn.close()

    async def init(self, migrations: Sequence[Migration] = MIGRATIONS) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with self.connect() as conn:
            await conn.execute("PRAGMA journal_mode = WAL")
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            )
            async with conn.execute("SELECT version FROM schema_migrations") as cursor:
                applied = {row["version"] async for row in cursor}
            for version, sql in sorted(migrations):
                if version in applied:
                    continue
                await conn.executescript(sql)
                await conn.execute("INSERT INTO schema_migrations (version) VALUES (?)", (version,))
                await conn.commit()
                logger.info("migration applied", extra={"version": version})

    async def ping(self) -> bool:
        try:
            async with self.connect() as conn:
                await conn.execute("SELECT 1")
            return True
        except Exception:
            logger.exception("database ping failed")
            return False
