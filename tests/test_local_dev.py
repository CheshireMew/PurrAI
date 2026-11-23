"""The explicit local profile uses real APIs and data, without production authentication or Telegram."""
import httpx

from scripts.local_dev import create_local_app, seed_demo
from purrai.app import create_app
from purrai.models import PointsPolicy


async def test_local_profile_is_isolated_and_seed_does_not_overwrite_saved_settings(env, monkeypatch):
    settings, db, tg, _ = env
    monkeypatch.setenv("PURRAI_DEV_DATABASE", str(db.url.split("///", 1)[-1]))
    monkeypatch.setenv("PURRAI_BOT_TOKEN", "999999:IGNORED_IN_LOCAL_PROFILE")
    monkeypatch.setenv("PURRAI_CHAT_API_KEY", "ignored-in-local-profile")
    app = create_local_app()
    assert not app.state.settings.bot_token and not app.state.settings.chat_api_key
    assert app.state.settings.superadmin_ids == [1]
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
            me = await client.get("/api/me")
            assert me.status_code == 200 and me.json()["local_dev"] is True
            groups = (await client.get("/api/groups")).json()
            assert any(group["id"] == -10010001 for group in groups)
            path = "/api/groups/-10010001/points/settings"
            document = (await client.get(path)).json()
            document["config"]["signin_points"] = 123
            saved = await client.put(path, json=document, headers={"Idempotency-Key": "local-dev-settings"})
            assert saved.status_code == 200, saved.text
            await seed_demo(app.state.db)
            async with app.state.db.sessions() as session:
                assert (await session.get(PointsPolicy, -10010001)).config["signin_points"] == 123
            assert (await client.get("/__local/status")).json()["mode"] == "local-development"

    # Production's normal app still requires a Telegram session.
    normal = create_app(settings, db, tg, start_runtime=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=normal), base_url="http://127.0.0.1") as client:
        assert (await client.get("/api/me")).status_code == 401


async def test_local_profile_rejects_nonlocal_clients_and_hosts(env, monkeypatch):
    _, db, _, _ = env
    monkeypatch.setenv("PURRAI_DEV_DATABASE", str(db.url.split("///", 1)[-1]))
    app = create_local_app()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("198.51.100.2", 1)), base_url="http://127.0.0.1") as client:
            assert (await client.get("/api/me")).status_code == 403
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://unrelated.example") as client:
            assert (await client.get("/api/me")).status_code == 403
    finally:
        await app.state.db.close()
