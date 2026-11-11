"""Owner-managed backup schedule, owned-file retention and read-only restore exercise."""

import asyncio
import json
import re
import shutil
import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from purrai.db import schema_head
from purrai.errors import ManagementError
from purrai.models import Meta, now
from purrai.runtime_alerts import update_episode
from purrai.schemas import BackupPlanInput


def inspect_backup(path, expected):
    """Restore into SQLite's private temporary database and verify the result."""
    with tempfile.TemporaryDirectory(prefix=".restore-exercise-", dir=path.parent) as temporary:
        return inspect_copy(path, expected, temporary)


def inspect_copy(path, expected, temporary):
    from pathlib import Path
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as source, closing(sqlite3.connect(Path(temporary) / "restored.sqlite3")) as restored:
        if shutil.disk_usage(path.parent).free < path.stat().st_size + 16 * 1024 * 1024:
            raise ValueError("恢复演练所需空间不足，未开始复制")
        source.backup(restored, pages=256, sleep=0.01)
        integrity = restored.execute("PRAGMA integrity_check").fetchall()
        foreign_keys = restored.execute("PRAGMA foreign_key_check").fetchall()
        tables = {row[0] for row in restored.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        revision = restored.execute("SELECT version_num FROM alembic_version").fetchone()[0] if "alembic_version" in tables else ""
        counts = {name: restored.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
                  for name in ("groups", "members", "verifications", "archived_messages", "actions") if name in tables}
        return {"integrity_ok": integrity == [("ok",)], "foreign_keys_ok": not foreign_keys,
                "revision": revision, "compatible": revision == expected,
                "restored": True, "counts": counts, "checked_at": now()}


class BackupManagement:
    def __init__(self, context):
        self.context = context
        self.lock = asyncio.Lock()

    def path(self, name):
        root = self.context.settings.backup_dir.resolve()
        if not re.fullmatch(r"purrai-\d{8}-\d{6}-\d{6}\.sqlite3", name):
            raise ManagementError(422, "请选择备份列表中的 SQLite 文件")
        path = root / name
        if path.is_symlink() or not path.is_file() or path.resolve().parent != root:
            raise ManagementError(404, "备份文件不存在或不是普通备份文件")
        return path

    async def status(self, actor):
        await self.context.access.principal(actor, root=True)
        root = self.context.settings.backup_dir.resolve()
        root.mkdir(parents=True, exist_ok=True)
        async with self.context.db.sessions() as session:
            stored = await session.get(Meta, "backup:plan")
            plan = json.loads(stored.value) if stored else BackupPlanInput(request_id="defaults").model_dump(exclude={"request_id"})
            records = {row.key.removeprefix("backup:record:"): json.loads(row.value)
                       for row in await session.scalars(select(Meta).where(Meta.key.startswith("backup:record:")))}
            inspections = {row.key.removeprefix("backup:inspection:"): json.loads(row.value)
                           for row in await session.scalars(select(Meta).where(Meta.key.startswith("backup:inspection:")))}
            state = await session.get(Meta, "backup:schedule-state")
        items = []
        for path in sorted(root.glob("purrai-*.sqlite3"), reverse=True):
            if path.is_symlink() or not path.is_file() or not re.fullmatch(r"purrai-\d{8}-\d{6}-\d{6}\.sqlite3", path.name):
                continue
            stat = path.stat()
            record = records.get(path.name, {})
            items.append({"file": path.name, "bytes": stat.st_size, "created_at": stat.st_mtime,
                          "managed": bool(record), "exercise": record.get("exercise") or inspections.get(path.name),
                          "revision": record.get("revision", inspections.get(path.name, {}).get("revision", "待演练核实"))})
        return {"plan": plan, "items": items, "free_bytes": shutil.disk_usage(root).free,
                "schedule": json.loads(state.value) if state else {}}

    async def plan(self, actor, body):
        await self.context.access.principal(actor, root=True)
        value = body.model_dump(exclude={"request_id"})
        intent = {"operation": "backup_plan", "body": value}
        async with self.context.db.transaction() as session:
            previous = await self.context.previous(session, actor, body.request_id, intent)
            if previous is not None:
                return previous
            row = await session.get(Meta, "backup:plan")
            if row:
                row.value = json.dumps(value)
            else:
                session.add(Meta(key="backup:plan", value=json.dumps(value)))
            return await self.context.record(session, actor, None, "backup_plan", "设置备份计划与保留授权", value, body.request_id, intent)

    async def create(self, actor=None, request_id="", exercise=False):
        if actor is not None:
            await self.context.access.principal(actor, root=True)
        async with self.lock:
            async with self.context.db.sessions() as session:
                previous = await self.context.previous(session, actor, request_id)
                if previous is not None:
                    if exercise and previous.get("exercise") and not all(previous["exercise"].get(key) for key in ("integrity_ok", "foreign_keys_ok", "compatible")):
                        raise ValueError("已保存的备份恢复演练未通过，请检查备份文件")
                    return previous
            path = await self.context.db.backup(self.context.settings.backup_dir)
            stat = path.stat()
            record = {"bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns, "created_at": now(), "revision": schema_head()}
            if exercise:
                record["exercise"] = await asyncio.to_thread(inspect_backup, path.resolve(), schema_head())
            async with self.context.db.transaction() as session:
                session.add(Meta(key="backup:record:" + path.name, value=json.dumps(record)))
                last = await session.get(Meta, "last_backup")
                if last:
                    last.value = str(path)
                else:
                    session.add(Meta(key="last_backup", value=str(path)))
                result = await self.context.record(session, actor, None, "backup", "生成一致性数据库备份",
                    {"file": path.name, "created_at": record["created_at"], "bytes": stat.st_size,
                     "exercise": record.get("exercise")}, request_id)
            if exercise and not all(record["exercise"].get(key) for key in ("integrity_ok", "foreign_keys_ok", "compatible")):
                raise ValueError("备份文件已保留，但恢复演练未通过；未整理旧备份")
            return result

    async def exercise(self, actor, body):
        await self.context.access.principal(actor, root=True)
        async with self.lock:
            intent = {"operation": "backup_exercise", "file": body.file}
            async with self.context.db.sessions() as session:
                previous = await self.context.previous(session, actor, body.request_id, intent)
                if previous is not None:
                    return previous
            path = self.path(body.file)
            before = path.stat()
            try:
                result = await asyncio.to_thread(inspect_backup, path, schema_head())
            except (sqlite3.Error, ValueError) as exc:
                raise ManagementError(422, f"备份恢复演练失败：{exc}") from exc
            after = path.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ManagementError(409, "演练期间备份文件已变化，请重新核实")
            async with self.context.db.transaction() as session:
                marker = await session.get(Meta, "backup:record:" + body.file)
                record = json.loads(marker.value) if marker else {"managed": False}
                record["exercise"] = result
                if marker:
                    marker.value = json.dumps(record)
                else:
                    # Inspection alone does not grant ownership/deletion rights.
                    inspection = await session.get(Meta, "backup:inspection:" + body.file)
                    if inspection:
                        inspection.value = json.dumps(result)
                    else:
                        session.add(Meta(key="backup:inspection:" + body.file, value=json.dumps(result)))
                return await self.context.record(session, actor, None, "backup_exercise", "备份恢复演练",
                    {"file": body.file, **result}, body.request_id, intent)

    async def prune(self, plan):
        """Only an explicitly enabled plan can retire its own unchanged files."""
        if not plan["prune_enabled"]:
            return
        async with self.lock:
            async with self.context.db.sessions() as session:
                records = [(row.key, json.loads(row.value)) for row in await session.scalars(select(Meta).where(
                    Meta.key.startswith("backup:record:")))]
            owned = []
            for key, record in records:
                if record.get("retired"):
                    continue
                try:
                    path = self.path(key.removeprefix("backup:record:"))
                    stat = path.stat()
                    if (stat.st_size, stat.st_mtime_ns) != (record.get("bytes"), record.get("mtime_ns")):
                        continue
                    owned.append((path, record, key))
                except ManagementError:
                    continue
            owned.sort(key=lambda item: item[1]["created_at"], reverse=True)
            total = sum(item[1]["bytes"] for item in owned)
            retired = []
            # Retain at least the newest successful backup regardless of caps.
            for index in range(len(owned) - 1, 0, -1):
                path, record, key = owned[index]
                if (index >= plan["retention_count"] or record["created_at"] < now() - plan["retention_days"] * 86400
                        or total > plan["max_total_mb"] * 1048576):
                    path.unlink()
                    total -= record["bytes"]
                    retired.append((key, {**record, "retired": True, "retired_at": now()}))
            if retired:
                async with self.context.db.transaction() as session:
                    for key, value in retired:
                        (await session.get(Meta, key)).value = json.dumps(value)
                    await self.context.record(session, None, None, "backup_retention", "按所有者保留授权整理备份",
                                              {"files": [key.removeprefix("backup:record:") for key, _ in retired]})

    async def cycle(self):
        async with self.context.db.sessions() as session:
            marker = await session.get(Meta, "backup:plan")
            state_row = await session.get(Meta, "backup:schedule-state")
            state = json.loads(state_row.value) if state_row else {}
        if not marker:
            return
        plan = json.loads(marker.value)
        current = datetime.fromtimestamp(now(), ZoneInfo("Asia/Shanghai"))
        last_date = datetime.fromtimestamp(state["last_success"], ZoneInfo("Asia/Shanghai")).date() if state.get("last_success") else None
        if (not plan["enabled"] or current.hour < plan["hour_beijing"] or state.get("retry_at", 0) > now()
                or last_date is not None and (current.date() - last_date).days < plan["interval_days"]):
            return
        try:
            await self.create(request_id="schedule:" + current.strftime("%Y%m%d"), exercise=plan["exercise_after_backup"])
            await self.prune(plan)
            state = {"last_success": now(), "error": "", "retry_at": 0}
        except Exception as exc:
            state = {**state, "error": str(exc)[:300], "retry_at": now() + 900}
        async with self.context.db.transaction() as session:
            marker = await session.get(Meta, "backup:schedule-state")
            if marker:
                marker.value = json.dumps(state)
            else:
                session.add(Meta(key="backup:schedule-state", value=json.dumps(state)))
            await update_episode(session, "备份计划", state.get("error", ""), self.context.settings.superadmin_ids)
