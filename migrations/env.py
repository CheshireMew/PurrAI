import asyncio

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from purrai.config import Settings
from purrai.models import Base

config = context.config


def run(connection):
    context.configure(connection=connection, target_metadata=Base.metadata, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


async def online():
    settings = Settings()
    from pathlib import Path

    if settings.database_url.startswith("sqlite"):
        Path(settings.database_url.split("///", 1)[-1]).parent.mkdir(parents=True, exist_ok=True)
    engine = create_async_engine(settings.database_url)
    async with engine.connect() as connection:
        await connection.run_sync(run)
    await engine.dispose()


if context.is_offline_mode():
    context.configure(url=Settings().database_url, target_metadata=Base.metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(online())
