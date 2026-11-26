import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import text

from app.db import database
from app.db.database import MIGRATIONS_DIR, Database
from app.db.tables import Base


async def test_init_creates_database_and_is_idempotent(tmp_path):
    db = Database(tmp_path / "nested" / "app.db")

    await db.init()
    await db.init()

    assert await db.ping() is True


async def test_migrations_build_exactly_the_orm_schema(tmp_path):
    db = Database(tmp_path / "app.db")
    await db.init()

    async with db.engine.connect() as conn:
        revision = await conn.run_sync(lambda c: MigrationContext.configure(c).get_current_revision())
        differences = await conn.run_sync(lambda c: compare_metadata(MigrationContext.configure(c), Base.metadata))

    assert revision == ScriptDirectory(str(MIGRATIONS_DIR)).get_current_head()
    assert differences == []


async def test_failed_migration_leaves_no_partial_schema(tmp_path, monkeypatch):
    def broken_upgrade(connection):
        connection.exec_driver_sql("CREATE TABLE half_done (id INTEGER)")
        raise RuntimeError("migration failed")

    monkeypatch.setattr(database, "_upgrade_to_head", broken_upgrade)
    db = Database(tmp_path / "app.db")

    with pytest.raises(RuntimeError):
        await db.init()

    async with db.session() as session:
        tables = (await session.scalars(text("SELECT name FROM sqlite_master WHERE type = 'table'"))).all()
    assert "half_done" not in tables


async def test_transaction_rolls_back_on_error(tmp_path):
    db = Database(tmp_path / "app.db")
    await db.init()

    with pytest.raises(RuntimeError):
        async with db.transaction(immediate=True) as session:
            await session.execute(text("INSERT INTO mnb_currencies (code, fetched_on) VALUES ('EUR', '2026-01-01')"))
            raise RuntimeError("stop")

    async with db.session() as session:
        assert (await session.scalars(text("SELECT code FROM mnb_currencies"))).all() == []


async def test_ping_reports_failure_instead_of_raising(tmp_path):
    db = Database(tmp_path / "missing-dir" / "app.db")

    assert await db.ping() is False