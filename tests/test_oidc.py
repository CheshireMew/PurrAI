import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from authlib.integrations.httpx_client import AsyncOAuth2Client
from authlib.jose import JsonWebKey, jwt


@pytest.mark.parametrize(
    "mode", ["valid", "group_admin", "wrong_nonce", "expired", "not_authorized", "wrong_audience"]
)
async def test_oidc_validates_claims_and_uses_telegram_id_not_subject(env, client, monkeypatch, mode):
    settings, _, telegram, _ = env
    settings.oidc_client_id, settings.oidc_client_secret = "999", "secret"
    settings.session_secret = "s" * 32
    key = JsonWebKey.generate_key("RSA", 2048, is_private=True, options={"kid": "test"})
    metadata = {
        "authorization_endpoint": "https://oauth.telegram.org/auth",
        "token_endpoint": "https://oauth.telegram.org/token",
        "jwks_uri": "https://oauth.telegram.org/keys",
    }
    original_get = httpx.AsyncClient.get

    async def outgoing(self, url, *args, **kwargs):
        if str(url).startswith("https://oauth.telegram.org"):
            request = httpx.Request("GET", url)
            return httpx.Response(
                200,
                request=request,
                json={"keys": [key.as_dict(is_private=False)]} if request.url.path == "/keys" else metadata,
            )
        return await original_get(self, url, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "get", outgoing)
    response = await client.get("/auth/telegram/start")
    assert response.status_code == 307
    query = parse_qs(urlsplit(response.headers["location"]).query)
    assert query["code_challenge_method"] == ["S256"]
    claims = {
        "iss": "https://oauth.telegram.org",
        "aud": "999",
        "sub": "unrelated-oidc-subject",
        "id": 1,
        "exp": int(time.time()) + 300,
        "iat": int(time.time()),
        "nonce": query["nonce"][0],
    }
    if mode == "wrong_nonce":
        claims["nonce"] = "different"
    elif mode == "expired":
        claims["exp"] = int(time.time()) - 300
    elif mode == "not_authorized":
        claims["id"] = 300
    elif mode == "group_admin":
        claims["id"] = 300
        telegram.members[(-1001, 300)] = {"status": "administrator"}
    elif mode == "wrong_audience":
        claims["aud"] = "another-app"

    async def token(*args, **kwargs):
        assert kwargs["code_verifier"]
        return {"id_token": jwt.encode({"alg": "RS256", "kid": "test"}, claims, key).decode()}

    monkeypatch.setattr(AsyncOAuth2Client, "fetch_token", token)
    response = await client.get("/auth/telegram/callback", params={"state": query["state"][0], "code": "abc"})
    assert response.status_code == 307
    if mode in ("valid", "group_admin"):
        assert response.headers["location"] == "/"
        assert (
            "purrai_session=" in response.headers["set-cookie"]
            or len(response.headers.get_list("set-cookie")) == 2
        )
        me = (await client.get("/api/me")).json()
        assert me["id"] == (300 if mode == "group_admin" else 1)
        assert me["root"] is (mode == "valid")
    else:
        assert "login_error=" in response.headers["location"]
    replay = await client.get("/auth/telegram/callback", params={"state": query["state"][0], "code": "abc"})
    assert "login_error=" in replay.headers["location"]
