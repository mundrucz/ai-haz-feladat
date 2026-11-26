import json
from datetime import date, timedelta

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import text

from app.db import database
from app.db.database import MIGRATIONS_DIR, Database
from app.db.tables import Base


def _upgrade(connection, target: str) -> None:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.attributes["connection"] = connection
    command.upgrade(config, target)


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


async def test_stored_storno_is_no_longer_flagged_after_migration(tmp_path):
    db = Database(tmp_path / "app.db")
    old_warning = {"code": "non_positive_total", "field": "gross_total", "severity": "warning"}
    other_warning = {"code": "same_party", "field": None, "severity": "warning"}
    rows = {"storno": ("-1270", [old_warning]), "both": ("-10", [old_warning, other_warning]), "zero": ("0", [old_warning])}
    async with db.engine.begin() as conn:
        await conn.run_sync(lambda c: _upgrade(c, "0001"))
        for number, (gross, issues) in rows.items():
            await conn.execute(
                text(
                    "INSERT INTO invoices (content_hash, filename, invoice_number, seller_name, seller_tax_id,"
                    " buyer_name, buyer_tax_id, issue_date, performance_date, due_date, currency, gross_total,"
                    " status, issues) VALUES (:n, :n, :n, 'A', '1', 'B', '2', '2026-01-01', '2026-01-01',"
                    " '2026-01-31', 'HUF', :gross, 'needs_review', :issues)"
                ),
                {"n": number, "gross": gross, "issues": json.dumps(issues)},
            )

    await db.init()

    async with db.session() as session:
        result = await session.execute(text("SELECT invoice_number, status, issues FROM invoices"))
        migrated = {number: (status, [i["code"] for i in json.loads(issues)]) for number, status, issues in result}
    assert migrated == {
        "storno": ("ok", []),
        "both": ("needs_review", ["same_party"]),
        "zero": ("needs_review", ["zero_total"]),
    }


async def test_stored_future_performance_invoice_is_flagged_after_migration(tmp_path):
    db = Database(tmp_path / "app.db")
    future = (date.today() + timedelta(days=30)).isoformat()
    rows = {"past": "2026-01-01", "future": future}
    async with db.engine.begin() as conn:
        await conn.run_sync(lambda c: _upgrade(c, "0002"))
        for number, performance in rows.items():
            await conn.execute(
                text(
                    "INSERT INTO invoices (content_hash, filename, invoice_number, seller_name, seller_tax_id,"
                    " buyer_name, buyer_tax_id, issue_date, performance_date, due_date, currency, gross_total,"
                    " status, issues) VALUES (:n, :n, :n, 'A', '1', 'B', '2', '2026-01-01', :performance,"
                    " :performance, 'EUR', '100', 'ok', '[]')"
                ),
                {"n": number, "performance": performance},
            )

    await db.init()

    async with db.session() as session:
        result = await session.execute(text("SELECT invoice_number, status, issues FROM invoices"))
        migrated = {number: (status, [i["code"] for i in json.loads(issues)]) for number, status, issues in result}
    assert migrated == {"past": ("ok", []), "future": ("ok", ["future_performance"])}


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