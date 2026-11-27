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
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False, info={"database": self})
        # One writer serializes moderation, verification, and HTTP changes in this single-worker app.
        self.lock = asyncio.Lock()
        self.managed_backup_lock = asyncio.Lock()

        @event.listens_for(self.engine.sync_engine, "connect")
        def configure(connection, _):
            from purrai.message_parts import stored_message_text
            connection.create_function("purrai_message_text", 1, stored_message_text, deterministic=True)
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    @asynccontextmanager
    async def transaction(self):
        async with self.lock:
            async with self.sessions() as session:
                session.info["database_writer"] = True
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

    @staticmethod
    def backup_filename():
        from datetime import datetime, timezone

        return f"purrai-{datetime.now(timezone.utc):%Y%m%d-%H%M%S-%f}.sqlite3"

    async def backup(self, directory: Path, *, filename=None) -> Path:
        import re

        name = filename or self.backup_filename()
        if not re.fullmatch(r"purrai-\d{8}-\d{6}-\d{6}\.sqlite3", name):
            raise ValueError("备份文件名无效")
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / name
        source = self.url.split("///", 1)[-1]

        def copy():
            import os
            import shutil
            import tempfile
            origin_path = Path(source).resolve()
            if not origin_path.is_file():
                raise RuntimeError("待备份数据库不存在")
            if target.exists() or target.is_symlink():
                raise FileExistsError("备份目标已存在，未覆盖现有文件")
            needed = origin_path.stat().st_size + 16 * 1024 * 1024
            if shutil.disk_usage(directory).free < needed:
                raise RuntimeError("备份目录可用空间不足，尚未生成新备份")
            # A final filename always denotes a finished SQLite snapshot.
            # Publish without replacing an existing file, including another
            # attempt that completed after its awaiting task was cancelled.
            with tempfile.NamedTemporaryFile(prefix=f".{name}.", suffix=".partial", dir=directory, delete=False) as temporary:
                working = Path(temporary.name)
            try:
                with closing(sqlite3.connect(origin_path.as_uri() + "?mode=ro", uri=True)) as origin, closing(sqlite3.connect(working)) as destination:
                    origin.backup(destination, pages=256, sleep=0.01)
                os.link(working, target)
            finally:
                working.unlink(missing_ok=True)

        # sqlite's backup API maintains consistency while the live database
        # continues writing. Do not hold the application's only writer lock.
        await asyncio.to_thread(copy)
        return target
