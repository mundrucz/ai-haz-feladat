from app.db import Database


async def test_init_creates_database_and_is_idempotent(tmp_path):
    db = Database(tmp_path / "nested" / "app.db")

    await db.init()
    await db.init()

    assert await db.ping() is True


async def test_pending_migrations_are_applied_once(tmp_path):
    db = Database(tmp_path / "app.db")
    migrations = [(1, "CREATE TABLE a (id INTEGER PRIMARY KEY);")]
    await db.init(migrations)

    migrations.append((2, "CREATE TABLE b (id INTEGER PRIMARY KEY);"))
    await db.init(migrations)
    await db.init(migrations)

    async with db.connect() as conn:
        async with conn.execute("SELECT version FROM schema_migrations ORDER BY version") as cursor:
            versions = [row["version"] async for row in cursor]
        async with conn.execute("SELECT name FROM sqlite_master WHERE name IN ('a', 'b')") as cursor:
            tables = {row["name"] async for row in cursor}
    assert versions == [1, 2]
    assert tables == {"a", "b"}


async def test_ping_reports_failure_instead_of_raising(tmp_path):
    db = Database(tmp_path / "missing-dir" / "app.db")

    assert await db.ping() is False
