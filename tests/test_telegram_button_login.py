import hashlib
import hmac
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from sqlalchemy import func, select

from purrai.app import create_app
from purrai.models import LoginAttempt, Session


def signed(parameters, token="999:OFFLINE_BUTTON_LOGIN_TEST"):
    fields = {key: str(value) for key, value in parameters.items()}
    check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    fields["hash"] = hmac.new(hashlib.sha256(token.encode()).digest(), check.encode(), hashlib.sha256).hexdigest()
    return fields


@pytest.mark.parametrize("user_id", [1, 300])
async def test_signed_button_enters_admin_without_oidc_or_external_requests(env, monkeypatch, user_id):
    settings, database, telegram, _ = env
    settings.bot_token = "999:OFFLINE_BUTTON_LOGIN_TEST"
    settings.session_secret = "s" * 40
    settings.cookie_secure = True
    telegram.members[(-1001, 300)] = {"status": "administrator"}

    async def no_network(*args, **kwargs):
        raise AssertionError("Button login must not contact the OIDC service")

    app = create_app(settings, database, telegram, start_runtime=False)
    original_get = httpx.AsyncClient.get

    async def checked_get(client, url, *args, **kwargs):
        if str(url).startswith("https://oauth.telegram.org"):
            return await no_network()
        return await original_get(client, url, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "get", checked_get)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test") as client:
        response = await client.get("/auth/telegram/button", params=signed({
            "id": user_id, "auth_date": int(time.time()), "first_name": "管理员 · 测试", "username": "Example",
        }))
        assert response.status_code == 307 and response.headers["location"] == "/"
        cookie = next(value for value in response.headers.get_list("set-cookie") if value.startswith("purrai_session="))
        assert "Secure" in cookie and "HttpOnly" in cookie
        assert response.headers["cache-control"] == "no-store"
        me = (await client.get("/api/me")).json()
        assert me["id"] == user_id and me["root"] is (user_id == 1)


@pytest.mark.parametrize("mode", ["tampered", "expired", "future", "other_bot", "not_admin", "duplicate", "missing", "malformed"])
async def test_invalid_button_authorization_does_not_create_a_session(env, mode):
    settings, database, telegram, _ = env
    settings.bot_token, settings.session_secret = "999:OFFLINE_BUTTON_LOGIN_TEST", "s" * 40
    fields = {"id": 1, "auth_date": int(time.time()), "first_name": "测试"}
    if mode == "expired":
        fields["auth_date"] -= 301
    elif mode == "future":
        fields["auth_date"] += 40
    elif mode == "not_admin":
        fields["id"] = 700
    elif mode == "malformed":
        fields["id"] = "not-a-user"
    parameters = signed(fields, "888:ANOTHER_BOT" if mode == "other_bot" else settings.bot_token)
    if mode == "tampered":
        parameters["id"] = "2"
    elif mode == "missing":
        parameters = {}
    elif mode == "duplicate":
        parameters = [*parameters.items(), ("id", "2")]
    app = create_app(settings, database, telegram, start_runtime=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/auth/telegram/button", params=parameters)
    assert "login_error" in parse_qs(urlsplit(response.headers["location"]).query)
    assert not any(value.startswith("purrai_session=") for value in response.headers.get_list("set-cookie"))
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Session)) == 0


async def test_button_authorization_is_consumed_once_and_survives_oidc_cleanup(env):
    settings, database, telegram, _ = env
    settings.bot_token, settings.session_secret = "999:OFFLINE_BUTTON_LOGIN_TEST", "s" * 40
    parameters = signed({"id": 1, "auth_date": int(time.time())})
    app = create_app(settings, database, telegram, start_runtime=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/auth/telegram/button", params=parameters)
        assert response.headers["location"] == "/"
        response = await client.get("/auth/telegram/button", params=parameters)
        assert "login_error" in parse_qs(urlsplit(response.headers["location"]).query)
    async with database.transaction() as session:
        from sqlalchemy import delete
        await session.execute(delete(LoginAttempt).where(LoginAttempt.expires_at <= time.time()))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/auth/telegram/button", params=parameters)
        assert "login_error" in parse_qs(urlsplit(response.headers["location"]).query)
    async with database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Session)) == 1
