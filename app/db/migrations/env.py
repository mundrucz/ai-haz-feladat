"""Alembic-környezet.

Az alkalmazás indításkor a saját kapcsolatát adja át (`Database.init`), így a migráció az ő tranzakciójában fut.
A parancssori `alembic` (pl. új revízió generálása) az `alembic.ini`-ben megadott adatbázishoz kapcsolódik.
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from alembic.autogenerate.api import AutogenContext
from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.types import TypeDecorator

from app.db.tables import Base

config = context.config
target_metadata = Base.metadata


def render_item(kind: str, obj: object, autogen_context: AutogenContext) -> str | bool:
    """A saját oszloptípus az adatbázis-típusaként kerül a revízióba, így a migráció nem függ az alkalmazás kódjától."""
    if kind == "type" and isinstance(obj, TypeDecorator):
        return f"sa.{type(obj.impl_instance).__name__}()"
    return False


def run_migrations(connection: Connection) -> None:
    # SQLite-ban az ALTER TABLE korlátozott: a batch mód a táblát újraépítve módosít. A DDL tranzakcióban fut
    # (a `BEGIN`-t a `Database` adja ki), így egy elszállt migráció nem hagy félkész sémát.
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,
        transactional_ddl=True,
        render_item=render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_from_url() -> None:
    engine = create_async_engine(config.get_main_option("sqlalchemy.url"))
    async with engine.connect() as connection:
        await connection.run_sync(run_migrations)
    await engine.dispose()


if (connection := config.attributes.get("connection")) is not None:
    run_migrations(connection)
else:
    if config.config_file_name is not None:
        fileConfig(config.config_file_name)
    asyncio.run(run_migrations_from_url())
