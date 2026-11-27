"""Regressions for the project audit's recovery and settlement failures."""

import asyncio
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest
from sqlalchemy import func, select

from conftest import message
from purrai import points_rewards
from purrai.db import Database
from purrai.lottery import cycle
from purrai.management import Management
from purrai.models import Group, LotteryEntry, Meta, PointsAccount, PointsEntry, PointsPacket, now
from purrai.points_rewards import create_packet, credit, user_account
from purrai.points_policy import get_policy
from purrai.points import PointsLedger
from purrai.points_schemas import MAX_POINTS, PointsIdentity


@pytest.mark.parametrize("existing", [False, True])
async def test_restore_cli_handles_missing_and_existing_targets(env, tmp_path, existing):
    settings, db, _, _ = env
    snapshot = await db.backup(tmp_path / "source-backup")
    target = Path(settings.database_url.split("///", 1)[-1]) if existing else tmp_path / "new" / "restored.sqlite3"
    if existing:
        async with db.transaction() as session:
            (await session.get(Group, -1001)).title = "恢复前仍须保留的内容"
    await db.close()
    destination_backups = tmp_path / "target-backups"
    environment = {**os.environ, "PURRAI_DATABASE_URL": f"sqlite+aiosqlite:///{target}",
                   "PURRAI_BACKUP_DIR": str(destination_backups), "PYTHONUTF8": "1"}
    result = await asyncio.to_thread(subprocess.run,
        [sys.executable, "-B", "-m", "purrai.cli", "restore", str(snapshot)],
        cwd=Path(__file__).resolve().parents[1], env=environment, capture_output=True, text=True,
        encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stderr
    restored = Database(environment["PURRAI_DATABASE_URL"])
    try:
        assert (await restored.schema_health())["ready"]
        async with restored.sessions() as session:
            assert (await session.get(Group, -1001)).title == "测试群"
    finally:
        await restored.close()
    if existing:
        kept = list(destination_backups.glob("*.sqlite3"))
        assert len(kept) == 1
        with sqlite3.connect(kept[0]) as connection:
            assert connection.execute("SELECT title FROM groups WHERE id=-1001").fetchone()[0] == "恢复前仍须保留的内容"


@pytest.mark.parametrize("existing", [False, True])
async def test_restore_cli_rejects_foreign_sqlite_without_changing_target(env, tmp_path, existing):
    settings, db, _, _ = env
    target = Path(settings.database_url.split("///", 1)[-1]) if existing else tmp_path / "missing.sqlite3"
    foreign = tmp_path / "foreign.sqlite3"
    with sqlite3.connect(foreign) as connection:
        connection.execute("CREATE TABLE unrelated (value TEXT)")
    await db.close()
    before = target.read_bytes() if existing else None
    result = await asyncio.to_thread(subprocess.run,
        [sys.executable, "-B", "-m", "purrai.cli", "restore", str(foreign)],
        cwd=Path(__file__).resolve().parents[1], env={**os.environ,
            "PURRAI_DATABASE_URL": f"sqlite+aiosqlite:///{target}",
            "PURRAI_BACKUP_DIR": str(tmp_path / "rejected-backups"), "PYTHONUTF8": "1"},
        capture_output=True, timeout=20)
    assert result.returncode != 0
    assert target.read_bytes() == before if existing else not target.exists()


async def test_interrupted_restore_rolls_back_native_sqlite_copy(env, tmp_path):
    settings, db, _, _ = env
    snapshot = await db.backup(tmp_path / "snapshot")
    async with db.transaction() as session:
        (await session.get(Group, -1001)).title = "中断恢复后必须保留"
    await db.close()
    marker = tmp_path / "restore-copy-started"
    probe = tmp_path / "interrupt_restore.py"
    probe.write_text('''import runpy, sqlite3, sys, time
from pathlib import Path
marker, snapshot_uri, snapshot = sys.argv[1:]
original = sqlite3.connect
class Observed(sqlite3.Connection):
    def backup(self, target, **kwargs):
        if self.origin.startswith(snapshot_uri):
            def progress(status, remaining, total):
                if remaining > 0:
                    Path(marker).write_text("native-copy-in-progress")
                    while True:
                        time.sleep(0.1)
            return super().backup(target, pages=1, progress=progress)
        return super().backup(target, **kwargs)
def connect(database, *args, **kwargs):
    kwargs["factory"] = Observed
    connection = original(database, *args, **kwargs)
    connection.origin = str(database)
    return connection
sqlite3.connect = connect
sys.argv = ["purrai", "restore", snapshot]
runpy.run_module("purrai.cli", run_name="__main__")
''', encoding="utf-8")
    process = subprocess.Popen([sys.executable, "-B", str(probe), str(marker), snapshot.resolve().as_uri(), str(snapshot)],
        cwd=Path(__file__).resolve().parents[1], env={**os.environ,
            "PURRAI_DATABASE_URL": settings.database_url,
            "PURRAI_BACKUP_DIR": str(tmp_path / "preserved"), "PYTHONUTF8": "1"},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        async with asyncio.timeout(5):
            while not marker.exists():
                assert process.poll() is None
                await asyncio.sleep(0.01)
    finally:
        if process.poll() is None:
            process.kill()
        await asyncio.to_thread(process.communicate, timeout=5)
    assert process.returncode != 0
    reopened = Database(settings.database_url)
    try:
        assert (await reopened.schema_health())["ready"]
        async with reopened.sessions() as session:
            assert (await session.get(Group, -1001)).title == "中断恢复后必须保留"
    finally:
        await reopened.close()


@pytest.mark.parametrize("unavailable", [False, True])
async def test_packet_refund_cap_keeps_pending_and_recovers_once_after_restart(client, env, monkeypatch, unavailable):
    from test_points_bot import configure
    await configure(client)
    settings, db, _, _ = env
    async with db.transaction() as session:
        account = await user_account(session, -1001, {"id": 42, "first_name": "发送者"})
        await credit(session, account, MAX_POINTS, "refund-fund", "adjustment", "测试入账")
        packet = await create_packet(session, account, await get_policy(session, -1001), message(301, ""), ["10"])
        if not unavailable:
            await credit(session, account, 10, "refund-top-up", "adjustment", "测试补充余额")
        identifier = packet.id
        account_id, version = account.id, account.version
    if unavailable:
        # Current management correctly forbids unbinding an outstanding packet.
        # Inject an external storage change after the real packet was saved;
        # the consumer must retain its refund rather than lose it or crash.
        await db.close()
        with closing(sqlite3.connect(settings.database_url.split("///", 1)[-1])) as external:
            external.execute("UPDATE points_accounts SET user_id=NULL, version=version+1 WHERE id=?", (account_id,))
            external.commit()
    monkeypatch.setattr(points_rewards, "now", lambda: now() + 8 * 86400)
    async with db.transaction() as session:
        await points_rewards.refund_expired(session)
    async with db.sessions() as session:
        packet = await session.get(PointsPacket, identifier)
        assert not packet.refunded and packet.remaining == 10
        assert await session.scalar(select(func.count()).select_from(PointsEntry).where(
            PointsEntry.request_id == f"packet-refund:{identifier}")) == 0
    await db.close()
    reopened = Database(settings.database_url)
    try:
        if unavailable:
            async with reopened.sessions() as session:
                version = (await session.get(PointsAccount, account_id)).version
            await PointsLedger(reopened).change(-1001, account_id,
                PointsIdentity(user_id=42, display_name="发送者", reason="测试身份已核实", version=version),
                1, "refund-rebind", identity=True)
        else:
            async with reopened.transaction() as session:
                account = await session.get(PointsAccount, account_id)
                await credit(session, account, -10, "refund-use", "adjustment", "测试使用积分")
        for _ in range(2):
            async with reopened.transaction() as session:
                await points_rewards.refund_expired(session)
        async with reopened.sessions() as session:
            assert (await session.get(PointsPacket, identifier)).refunded
            assert await session.scalar(select(PointsAccount.balance).where(PointsAccount.user_id == 42)) == MAX_POINTS
            assert await session.scalar(select(func.count()).select_from(PointsEntry).where(
                PointsEntry.request_id == f"packet-refund:{identifier}")) == 1
    finally:
        await reopened.close()


async def test_maintenance_failure_does_not_block_other_cleanup(client, env, monkeypatch):
    from purrai.action_queue import enqueue
    from purrai import lottery
    from purrai.models import Action
    from test_lottery import create, enable_points, join, operate
    from test_verification import fixed_challenge
    _, db, tg, service = env
    await enable_points(client)
    row = await operate(client, await create(client, draw_mode="time", ends_at=now() + 120,
        prizes=[{"name": "积分", "kind": "points", "points": 10}]), "publish")
    await join(db, row)
    verification = await fixed_challenge(env, monkeypatch)
    async with db.transaction() as session:
        notice = await enqueue(session, "admin_notify", "audit:recover-notice", -1001,
                               payload={"chat_id": -1001, "text": "操作已完成"})
    assert await service.run_one_action(include_cleanup=False)
    async with db.transaction() as session:
        receipt = await session.get(Action, notice.id)
        notice_message_id = receipt.payload["delivery"]["result"]["message_id"]
        notice_cleanup = await session.scalar(select(Action).where(
            Action.dedupe_key == "cleanup:notice:" + notice.id))
        assert notice_cleanup is not None
        # Reproduce an old confirmed receipt whose cleanup intent was lost.
        await session.delete(notice_cleanup)
    after_deadline = max(now() + 180, verification.deadline + 1)
    monkeypatch.setattr(lottery, "now", lambda: after_deadline)
    monkeypatch.setattr("purrai.verification_cleanup.now", lambda: after_deadline)
    monkeypatch.setattr("purrai.actions.now", lambda: after_deadline)
    async def unavailable(session):
        raise OSError("injected refund storage failure")
    monkeypatch.setattr(points_rewards, "refund_expired", unavailable)
    async with db.transaction() as session:
        session.add(Meta(key="audit:independent-expiry", value="test", expires_at=now() - 60))
        for kind, identifier in (("command", 880), ("notice", 881)):
            await enqueue(session, "delete", f"cleanup:{kind}:audit", -1001,
                          payload={"chat_id": -1001, "message_id": identifier})
    task = asyncio.create_task(service.cleanup_loop())
    try:
        async with asyncio.timeout(1.5):
            while True:
                async with db.sessions() as session:
                    deleted = {data["message_id"] for method, data in tg.calls if method == "delete_message"}
                    expected = {880, 881, verification.message_id, notice_message_id}
                    if await session.get(Meta, "audit:independent-expiry") is None and expected <= deleted:
                        break
                await asyncio.sleep(0.01)
        async with db.sessions() as session:
            assert await session.scalar(select(PointsAccount.balance).where(PointsAccount.user_id == 42)) == 10
            for key in (f"cleanup:{verification.id}:{verification.message_id}", "cleanup:notice:" + notice.id):
                assert (await session.scalar(select(Action).where(Action.dedupe_key == key))).state == "succeeded"
        assert sum(method == "send_message" and data.get("text") == "操作已完成"
                   for method, data in tg.calls) == 1
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.parametrize("settlement", ["prize", "refund"])
async def test_lottery_blocked_prefix_does_not_starve_later_batches(client, env, settlement):
    from test_lottery import create, enable_points, join, operate
    settings, db, _, _ = env
    await enable_points(client)
    users = range(10000, 10203)
    async with db.transaction() as session:
        for user in users:
            account = await user_account(session, -1001, {"id": user, "first_name": "参与者"})
            await credit(session, account, MAX_POINTS, f"lottery-fund:{user}", "adjustment", "测试入账")
    options = {"mode": "instant", "entry_cost": 1, "prizes": [
        {"name": "积分奖", "kind": "points", "points": 10, "quantity": 203}]} if settlement == "prize" else {"entry_cost": 1}
    row = await operate(client, await create(client, **options), "publish")
    for user in users:
        await join(db, row, user=user)
    if settlement == "refund":
        async with db.transaction() as session:
            for user in users:
                account = await session.scalar(select(PointsAccount).where(PointsAccount.user_id == user))
                await credit(session, account, 1, f"lottery-top-up:{user}", "adjustment", "测试补充余额")
        await operate(client, row, "cancel")
    async with db.transaction() as session:
        entries = list(await session.scalars(select(LotteryEntry).where(LotteryEntry.lottery_id == row["id"])))
        eligible = entries[100:]
        for entry in eligible:
            account = await session.scalar(select(PointsAccount).where(PointsAccount.user_id == entry.user_id))
            await credit(session, account, -9 if settlement == "prize" else -1,
                         f"lottery-use:{entry.id}", "adjustment", "测试使用积分")
        eligible_ids = {entry.id for entry in eligible}
        eligible_users = {entry.user_id for entry in eligible}
    await db.close()
    reopened = Database(settings.database_url)
    try:
        arriving = await operate(client, await create(client, key="new-arrivals", mode="instant", entry_cost=1,
            prizes=[{"name": "新增积分奖", "kind": "points", "points": 10, "quantity": 3}]), "publish")
        arriving_ids = set()
        for index in range(3):
            if index:
                user = 20000 + index
                async with db.transaction() as session:
                    account = await user_account(session, -1001, {"id": user, "first_name": "新参与者"})
                    await credit(session, account, MAX_POINTS, f"new-fund:{user}", "adjustment", "测试入账")
                entry = await join(db, arriving, user=user)
                arriving_ids.add(entry["id"])
                async with db.transaction() as session:
                    account = await session.scalar(select(PointsAccount).where(PointsAccount.user_id == user))
                    await credit(session, account, -9, f"new-use:{user}", "adjustment", "测试使用积分")
            async with reopened.transaction() as session:
                await cycle(session)
        async with reopened.sessions() as session:
            settled = set(await session.scalars(select(LotteryEntry.id).where(
                LotteryEntry.id.in_(eligible_ids), LotteryEntry.fulfilled.is_(True)
                if settlement == "prize" else LotteryEntry.state == "refunded")))
            assert settled == eligible_ids
            assert await session.scalar(select(func.count()).select_from(PointsEntry).where(
                PointsEntry.kind == ("lottery_prize" if settlement == "prize" else "lottery_refund"),
                PointsEntry.account_id.in_(select(PointsAccount.id).where(PointsAccount.user_id.in_(eligible_users))))) == 103
        detail = (await client.get(f"/api/groups/-1001/lotteries/{row['id']}"))
        assert detail.status_code == 200
        new_detail = (await client.get(f"/api/groups/-1001/lotteries/{arriving['id']}" )).json()
        assert {entry["id"] for entry in new_detail["entries"] if entry["fulfilled"]} == arriving_ids
    finally:
        await reopened.close()


async def test_managed_backup_reuses_file_after_receipt_failure_and_restart(env, monkeypatch):
    settings, db, tg, _ = env
    management = Management(db, tg, settings=settings)
    async def fail_receipt(*args, **kwargs):
        raise OSError("injected receipt commit failure")
    monkeypatch.setattr(management.backups.context, "record", fail_receipt)
    with pytest.raises(OSError):
        await management.backups.create(1, "same-backup-intent")
    files_before = set(settings.backup_dir.glob("*.sqlite3"))
    assert len(files_before) == 1
    await db.close()
    reopened = Database(settings.database_url)
    try:
        resumed = Management(reopened, tg, settings=settings)
        result = await resumed.backups.create(1, "same-backup-intent")
        assert set(settings.backup_dir.glob("*.sqlite3")) == files_before
        assert settings.backup_dir / result["file"] in files_before
        items = (await resumed.backups.status(1))["items"]
        assert len(items) == 1 and items[0]["managed"]
        with sqlite3.connect(settings.backup_dir / result["file"]) as backup:
            assert backup.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    finally:
        await reopened.close()


async def test_backup_http_retries_same_intent_without_duplicate_file(client, env):
    settings, _, _, _ = env
    path = "/api/system/backup"
    assert (await client.post(path)).status_code == 422
    headers = {"Idempotency-Key": "http-backup-same-intent"}
    first = await client.post(path, headers=headers)
    assert first.status_code == 200, first.text
    repeated = await client.post(path, headers=headers)
    assert repeated.status_code == 200 and repeated.json() == first.json()
    assert len(list(settings.backup_dir.glob("*.sqlite3"))) == 1


async def test_bot_backup_callback_replay_uses_the_same_completed_file(env):
    from purrai.runtime import Runtime
    from test_bot_admin import click, deliver, private, texts
    settings, db, tg, service = env
    runtime = Runtime(db, tg, service)
    try:
        await deliver(runtime, {"message": private("/admin")})
        await click(runtime, tg, "系统状态与备份")
        query = await click(runtime, tg, "生成一致性备份")
        files = set(settings.backup_dir.glob("*.sqlite3"))
        assert len(files) == 1
        assert any(next(iter(files)).name in text and "已保存" in text for text in texts(tg))
        await deliver(runtime, {"callback_query": query})
        assert set(settings.backup_dir.glob("*.sqlite3")) == files
    finally:
        await runtime.stop()


async def test_scheduled_backup_recovers_receipt_failure_without_second_file(env, monkeypatch):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from purrai import backup_management
    from purrai.schemas import BackupPlanInput
    settings, db, tg, _ = env
    clock = {"value": datetime.fromtimestamp(now(), ZoneInfo("Asia/Shanghai")).replace(
        hour=12, minute=0, second=0, microsecond=0).timestamp()}
    monkeypatch.setattr(backup_management, "now", lambda: clock["value"])
    management = Management(db, tg, settings=settings)
    await management.backups.plan(1, BackupPlanInput(request_id="schedule-plan", enabled=True, hour_beijing=0))
    original = management.backups.context.record
    async def fail_backup(*args, **kwargs):
        if len(args) > 3 and args[3] == "backup":
            raise OSError("injected scheduled receipt failure")
        return await original(*args, **kwargs)
    monkeypatch.setattr(management.backups.context, "record", fail_backup)
    await management.backups.cycle()
    files = set(settings.backup_dir.glob("*.sqlite3"))
    assert len(files) == 1
    assert (await management.backups.status(1))["pending"]
    monkeypatch.setattr(management.backups.context, "record", original)
    clock["value"] += 901
    await management.backups.cycle()
    await management.backups.cycle()
    status = await management.backups.status(1)
    assert set(settings.backup_dir.glob("*.sqlite3")) == files
    assert not status["pending"] and not status["schedule"]["error"]
    assert status["items"][0]["managed"] and status["items"][0]["exercise"]["compatible"]
