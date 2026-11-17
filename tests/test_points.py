import asyncio

import pytest
from sqlalchemy import func, select

from purrai.errors import ManagementError
from purrai.models import Group, PointsAccount, PointsEntry, PointsImport
from purrai.points import PointsLedger
from purrai.points_schemas import PointsAdjustment, PointsImportInput


def sample():
    return {"batch_key": "legacy-001", "source": "历史账本截图", "rows": [
        {"source_key": "old:1", "display_name": "同名", "points": 100, "source_rank": 1},
        {"source_key": "old:2", "display_name": "同名", "points": 70, "source_rank": 2},
        {"source_key": "old:3", "display_name": "", "points": 30, "source_rank": 3}]}


async def test_import_preview_replay_conflicts_and_atomicity(client, env):
    _, db, telegram, _ = env
    path = "/api/groups/-1001/points"
    body = sample()
    response = await client.post(path + "/import/preview", json=body)
    assert response.status_code == 200 and response.json()["total_points"] == 200
    assert (await client.get(path)).json()["summary"]["accounts"] == 0
    first = await client.post(path + "/import", json=body)
    assert first.status_code == 200 and not first.json()["already_imported"]
    body["rows"].reverse()
    again = await client.post(path + "/import", json=body)
    assert again.json()["already_imported"]
    body["rows"][0]["points"] = 999
    assert (await client.post(path + "/import", json=body)).status_code == 409
    body["batch_key"] = "legacy-002"
    body["rows"].insert(0, {"source_key": "new:4", "display_name": "新增", "points": 4})
    assert (await client.post(path + "/import", json=body)).status_code == 409
    async with db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(PointsAccount)) == 3
        assert await session.scalar(select(func.count()).select_from(PointsEntry)) == 3
        assert await session.scalar(select(func.count()).select_from(PointsImport)) == 1
    assert not [call for call in telegram.calls if call[0].startswith("send_")]


async def test_adjust_replay_stale_balance_and_ledger(client):
    path = "/api/groups/-1001/points"
    await client.post(path + "/import", json=sample())
    row = (await client.get(path)).json()["items"][0]
    url = path + f"/{row['id']}/adjust"
    body = {"delta": -30, "reason": "兑换", "version": 1}
    header = {"Idempotency-Key": "adjust-0001"}
    first = await client.post(url, json=body, headers=header)
    assert first.status_code == 200 and first.json()["balance"] == 70
    assert (await client.post(url, json=body, headers=header)).json() == first.json()
    assert (await client.post(url, json={**body, "delta": 10}, headers=header)).status_code == 409
    assert (await client.post(url, json=body, headers={"Idempotency-Key": "adjust-stale"})).status_code == 409
    assert (await client.post(url, json={**body, "delta": -71, "version": 2}, headers={"Idempotency-Key": "overdraw-001"})).status_code == 409
    history = (await client.get(path + f"/{row['id']}/history")).json()
    assert history["total"] == 2
    assert sum(item["delta"] for item in history["items"]) == history["account"]["balance"] == 70


async def test_identity_duplicate_names_rebinding_and_isolation(client, env):
    _, db, _, _ = env
    path = "/api/groups/-1001/points"
    await client.post(path + "/import", json=sample())
    rows = (await client.get(path)).json()["items"]
    assert len(rows) == 3 and rows[0]["display_name"] == rows[1]["display_name"]
    url = path + f"/{rows[0]['id']}/identity"
    body = {"user_id": 123, "display_name": "改名", "reason": "核实本人", "version": 1}
    header = {"Idempotency-Key": "identity-001"}
    first = await client.put(url, json=body, headers=header)
    assert first.status_code == 200 and first.json()["balance"] == 100
    assert first.json()["previous_identity"] == {"user_id": None, "display_name": "同名"}
    assert (await client.put(url, json=body, headers=header)).json() == first.json()
    assert (await client.put(path + f"/{rows[1]['id']}/identity", json=body, headers={"Idempotency-Key": "identity-002"})).status_code == 409
    page = (await client.get(path + "?pending=true")).json()
    assert page["total"] == 2 and page["summary"] == {"accounts": 3, "points": 200, "pending": 2}
    assert (await client.get(path + "?query=123")).json()["items"][0]["id"] == rows[0]["id"]
    assert (await client.get(path + "?query=同名")).json()["items"][0]["rank"] == 2
    assert (await client.get(path + "?query=%25")).json()["total"] == 0
    response = await client.put(url, json={**body, "user_id": None, "version": 2}, headers={"Idempotency-Key": "identity-003"})
    assert response.status_code == 200 and response.json()["balance"] == 100
    async with db.transaction() as session:
        session.add(Group(id=-1002, approved=True, title="另一个群"))
    assert (await client.get(f"/api/groups/-1002/points/{rows[0]['id']}/history")).status_code == 404
    assert (await client.post(f"/api/groups/-1002/points/{rows[0]['id']}/adjust", json={"delta": 1, "version": 3, "reason": "test"}, headers={"Idempotency-Key": "cross-group1"})).status_code == 404


async def test_concurrent_adjustments_cannot_lose_updates(env):
    _, db, _, _ = env
    ledger = PointsLedger(db)
    await ledger.import_data(-1001, PointsImportInput(**sample()))
    row = (await ledger.page(-1001))["items"][0]
    async def change(key):
        try:
            return await ledger.change(-1001, row["id"], PointsAdjustment(delta=5, version=1, reason="奖励"), 1, key)
        except ManagementError as exc:
            return exc.status
    results = await asyncio.gather(change("concurrent-a"), change("concurrent-b"))
    assert sum(item == 409 for item in results) == 1
    assert (await ledger.history(-1001, row["id"]))["account"]["balance"] == 105


@pytest.mark.parametrize("change", [
    {"points": -1}, {"points": 1.5}, {"points": True}, {"source_key": " "}, {"points": 9007199254740992}])
async def test_invalid_import_leaves_no_accounts(client, change):
    body = sample()
    body["rows"][0].update(change)
    assert (await client.post("/api/groups/-1001/points/import", json=body)).status_code == 422
    assert (await client.get("/api/groups/-1001/points")).json()["total"] == 0


async def test_create_account_auth_and_pagination(client, env):
    path = "/api/groups/-1001/points"
    body = {"user_id": 42, "display_name": "新群友"}
    headers = {"Idempotency-Key": "create-account"}
    first = await client.post(path, json=body, headers=headers)
    assert first.status_code == 200 and first.json()["balance"] == 0
    assert (await client.post(path, json=body, headers=headers)).json() == first.json()
    assert (await client.post(path, json=body, headers={"Idempotency-Key": "create-another"})).status_code == 409
    body = sample()
    body["rows"] = [{"source_key": str(i), "display_name": f"member {i}", "points": 50-i, "source_rank": i+1} for i in range(25)]
    await client.post(path + "/import", json=body)
    second = (await client.get(path + "?page_number=2")).json()
    assert second["total"] == 26 and len(second["items"]) == 6 and second["items"][0]["rank"] == 21
    client.cookies.clear()
    assert (await client.get(path)).status_code == 401
    assert (await client.post(path + "/import", json=body)).status_code == 401


async def test_non_admin_cannot_manage_points(client, env):
    from purrai.models import Session
    _, db, _, _ = env
    async with db.transaction() as session:
        login = await session.scalar(select(Session))
        login.user_id = 555
    assert (await client.get("/api/groups/-1001/points")).status_code == 403


async def test_verified_import_ids_are_atomic_and_unique(client):
    path = "/api/groups/-1001/points"
    body = sample()
    body["rows"][0]["user_id"] = 123
    body["rows"][1]["user_id"] = 123
    assert (await client.post(path + "/import", json=body)).status_code == 422
    body["rows"][1]["user_id"] = 456
    assert (await client.post(path + "/import", json=body)).status_code == 200
    assert (await client.get(path)).json()["summary"]["pending"] == 1
    body["batch_key"] = "new-batch"
    for row in body["rows"]:
        row["source_key"] += "new"
    assert (await client.post(path + "/import", json=body)).status_code == 409
    assert (await client.get(path)).json()["total"] == 3


def test_points_migration(tmp_path):
    import os
    import sqlite3
    import subprocess
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    database = tmp_path / "points.sqlite3"
    environment = {**os.environ, "PURRAI_DATABASE_URL": f"sqlite+aiosqlite:///{database}"}
    before = None
    for target in ("0030", "0031"):
        run = subprocess.run([sys.executable, "-m", "alembic", "upgrade", target], cwd=root,
                             env=environment, capture_output=True, text=True, timeout=40)
        assert run.returncode == 0, run.stderr
        with sqlite3.connect(database) as connection:
            policies = connection.execute("SELECT settings,rules,business_rules FROM shared_policy").fetchall()
            if target == "0030":
                before = policies
            else:
                assert before == policies
                assert connection.execute("SELECT count(*) FROM points_accounts").fetchone()[0] == 0
                assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
