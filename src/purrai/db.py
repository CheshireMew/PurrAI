import asyncio
import sqlite3
from contextlib import asynccontextmanager
from contextlib import closing
from pathlib import Path
from functools import lru_cache

from sqlalchemy import event, inspect, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@lru_cache(maxsize=1)
def schema_head():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    return ScriptDirectory.from_config(Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))).get_current_head()


class Database:
    def __init__(self, url: str):
        self.url = url
        if url.startswith("sqlite"):
            name = url.split("///", 1)[-1]
            if name != ":memory:":
                Path(name).parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_async_engine(url)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        # One writer serializes moderation, verification, and HTTP changes in this single-worker app.
        self.lock = asyncio.Lock()

        @event.listens_for(self.engine.sync_engine, "connect")
        def configure(connection, _):
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    @asynccontextmanager
    async def transaction(self):
        async with self.lock:
            async with self.sessions() as session:
                async with session.begin():
                    yield session

    async def close(self):
        await self.engine.dispose()

    async def schema_health(self):
        from purrai.models import Base

        def inspect_schema(connection):
            inspector = inspect(connection)
            tables = set(inspector.get_table_names())
            missing_tables = sorted(set(Base.metadata.tables) - tables)
            missing_columns = {}
            for name, table in Base.metadata.tables.items():
                if name in tables:
                    missing = sorted(set(table.columns.keys()) - {c["name"] for c in inspector.get_columns(name)})
                    if missing:
                        missing_columns[name] = missing
            revisions = connection.execute(text("SELECT version_num FROM alembic_version")).scalars().all() if "alembic_version" in tables else []
            return {"ready": revisions == [schema_head()] and not missing_tables and not missing_columns,
                    "revision": revisions, "expected_revision": schema_head(),
                    "missing_tables": missing_tables, "missing_columns": missing_columns}

        async with self.engine.connect() as connection:
            return await connection.run_sync(inspect_schema)

    async def backup(self, directory: Path) -> Path:
        from datetime import datetime, timezone

        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"purrai-{datetime.now(timezone.utc):%Y%m%d-%H%M%S-%f}.sqlite3"
        source = self.url.split("///", 1)[-1]

        def copy():
            import shutil
            origin_path = Path(source).resolve()
            if not origin_path.is_file():
                raise RuntimeError("待备份数据库不存在")
            needed = origin_path.stat().st_size + 16 * 1024 * 1024
            if shutil.disk_usage(directory).free < needed:
                raise RuntimeError("备份目录可用空间不足，尚未生成新备份")
            with closing(sqlite3.connect(origin_path.as_uri() + "?mode=ro", uri=True)) as origin, closing(sqlite3.connect(target)) as destination:
                origin.backup(destination, pages=256, sleep=0.01)

        # sqlite's backup API maintains consistency while the live database
        # continues writing. Do not hold the application's only writer lock.
        await asyncio.to_thread(copy)
        return target
