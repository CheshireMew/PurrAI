import hashlib
import time

from purrai.models import Group, Session, default_settings


async def test_root_dashboard_and_config_validation(client):
    assert (await client.get("/api/me")).json()["root"] is True
    assert (await client.get("/api/dashboard")).json()["groups"] == 1
    response = await client.patch("/api/groups/-1001/settings", json={"settings": {"max_attempts": 0}})
    assert response.status_code == 422
    assert (await client.patch("/api/groups/-1001/settings", json={"enabled": True})).status_code == 422


async def test_authorization_csrf_expiration_and_revocation(env, client):
    _, database, telegram, _ = env
    telegram.members[(-1001, 2)] = {"status": "administrator"}
    async with database.transaction() as session:
        session.add(
            Session(
                id=hashlib.sha256(b"other").hexdigest(),
                user_id=2,
                csrf="other-csrf",
                expires_at=time.time() + 60,
            )
        )
        session.add(Group(approved=True, id=-1002, title="其他群", settings=default_settings()))
    client.cookies.set("purrai_session", "other")
    assert (await client.get("/api/admins")).status_code == 403
    assert (await client.get("/api/groups/-1002/rules")).status_code == 403
    assert (
        await client.post("/api/groups/-1001/rules", json={"kind": "keyword", "value": "广告"})
    ).status_code == 403
    client.headers["X-CSRF-Token"] = "other-csrf"
    assert (
        await client.post("/api/groups/-1001/rules", json={"kind": "keyword", "value": "广告"})
    ).status_code == 200
    telegram.members[(-1001, 2)] = {"status": "member"}
    assert (await client.get("/api/me")).status_code == 403
    async with database.transaction() as session:
        login = await session.get(Session, hashlib.sha256(b"other").hexdigest())
        login.expires_at = 0
    assert (await client.get("/api/me")).status_code == 401


async def test_rules_preview_and_whitelist(client):
    response = await client.post("/api/groups/-1001/rules", json={"kind": "keyword", "value": "广告"})
    assert response.status_code == 200
    identifier = response.json()["id"]
    assert (await client.post("/api/groups/-1001/rules/preview", json={"text": "广告"})).json()["reasons"]
    assert (
        await client.put(
            f"/api/groups/-1001/rules/{identifier}",
            json={"kind": "keyword", "value": "广告", "enabled": False},
        )
    ).status_code == 200
    assert not (await client.post("/api/groups/-1001/rules/preview", json={"text": "广告"})).json()["reasons"]
    assert (await client.put("/api/groups/-1001/whitelist", json={"user_id": 42})).status_code == 200
    assert len((await client.get("/api/groups/-1001/whitelist")).json()) == 1
    assert (await client.delete("/api/groups/-1001/whitelist/42")).status_code == 200


async def test_login_not_configured_and_health(client):
    assert (await client.get("/auth/telegram/start")).status_code == 503
    assert (await client.get("/health/live")).status_code == 200
    assert (await client.get("/health/ready")).status_code == 503
    assert (await client.get("/api/does-not-exist")).status_code == 404


async def test_permissions_missing_blocks_activation(env, client, monkeypatch):
    _, database, telegram, _ = env
    original = telegram.call

    async def missing(method, **kwargs):
        if method == "get_chat_member":
            return {"status": "member"}
        return await original(method, **kwargs)

    monkeypatch.setattr(telegram, "call", missing)
    assert (await client.post("/api/groups/-1001/activate")).status_code == 422


async def test_backup_and_restore(env, client):
    _, database, _, _ = env
    response = await client.post("/api/system/backup")
    assert response.status_code == 200
    import sqlite3

    source = env[0].backup_dir / response.json()["file"]
    with sqlite3.connect(source) as backup, sqlite3.connect(source.parent / "restored.sqlite3") as restored:
        backup.backup(restored)
        assert restored.execute("SELECT title FROM groups").fetchone()[0] == "测试群"


async def test_manual_batch_is_idempotent_and_history_survives_refresh(env, client, monkeypatch):
    from conftest import drain, join
    from purrai import captcha

    monkeypatch.setattr(captcha, "make_image", lambda: ("123456", b"png"))
    _, _, _, service = env
    await service.member_event(join(user_id=42))
    await service.member_event(join(user_id=43))
    await drain(service)
    body = {"action": "release", "reason": "核实后批量放行", "request_id": "batch-request-1"}
    first = (await client.post("/api/groups/-1001/release-pending", json=body)).json()
    await drain(service)
    second = (await client.post("/api/groups/-1001/release-pending", json=body)).json()
    assert len(first["action_ids"]) == 2
    assert first == second
    history = (await client.get("/api/groups/-1001/actions")).json()["items"]
    assert len(history) == 5  # Batch, two releases and two automatically recorded whitelist grants.
    assert len([row for row in history if row["kind"] == "whitelist"]) == 2
    assert all(row["actor_id"] == 1 and row["state"] == "succeeded" for row in history)


async def test_authorized_group_admin_write_succeeds_only_in_own_group(env, client):
    _, database, telegram, _ = env
    telegram.members[(-1001, 2)] = {"status": "administrator"}
    async with database.transaction() as session:
        session.add(
            Session(
                id=hashlib.sha256(b"authorized").hexdigest(),
                user_id=2,
                csrf="authorized-csrf",
                expires_at=time.time() + 60,
            )
        )
    client.cookies.set("purrai_session", "authorized")
    client.headers["X-CSRF-Token"] = "authorized-csrf"
    assert (
        await client.post("/api/groups/-1001/rules", json={"kind": "keyword", "value": "广告"})
    ).status_code == 200
    assert len((await client.get("/api/groups")).json()) == 1
    assert (await client.post("/api/groups/-1001/activate")).status_code == 200
    assert (await client.post("/api/system/backup")).status_code == 403
