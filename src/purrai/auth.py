import hashlib
import hmac
import secrets
import asyncio
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from purrai.errors import ManagementError
from purrai.models import LoginAttempt, Session, now


def telegram_button_identity(parameters, bot_token):
    fields = dict(parameters.multi_items())
    if len(fields) != len(parameters.multi_items()):
        raise ValueError("重复的授权字段")
    signature = fields.pop("hash", "")
    if len(signature) != 64 or any(char not in "0123456789abcdef" for char in signature):
        raise ValueError("缺少有效授权签名")
    check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret = hashlib.sha256(bot_token.encode()).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise ValueError("授权签名不匹配")
    user_id, timestamp = int(fields["id"]), int(fields["auth_date"])
    if user_id <= 0 or not -30 <= now() - timestamp <= 300:
        raise ValueError("授权已过期")
    return user_id, "telegram-button:" + hashlib.sha256(signature.encode()).hexdigest(), timestamp + 331


@dataclass
class Identity:
    id: int
    root: bool
    csrf: str


async def identity(request: Request) -> Identity:
    cookie = request.cookies.get("purrai_session", "")
    digest = hashlib.sha256(cookie.encode()).hexdigest()
    async with request.app.state.db.sessions() as session:
        login = await session.get(Session, digest)
        if not cookie or not login or login.expires_at <= now():
            raise HTTPException(401, "请先使用 Telegram 登录")
        root = login.user_id in request.app.state.settings.superadmin_ids
        if request.method not in ("GET", "HEAD", "OPTIONS") and not secrets.compare_digest(
            request.headers.get("x-csrf-token", ""), login.csrf
        ):
            raise HTTPException(403, "会话校验失败，请刷新页面后重试")
    return Identity(login.user_id, root, login.csrf)


async def root_identity(user: Identity = Depends(identity)):
    if not user.root:
        raise HTTPException(403, "此操作仅限超级管理员")
    return user


async def authorize_group(request: Request, user: Identity, group_id: int, write=False):
    await request.app.state.management.authorize(user.id, group_id, write=write)


async def group_ids(request: Request, user: Identity) -> list[int] | None:
    if user.root:
        return None
    groups = await request.app.state.management.groups(user.id)
    if not groups:
        raise ManagementError(403, "你目前不是机器人已加入群的管理员，请先在 Telegram 群中设置管理员身份")
    return [row["id"] for row in groups]


def install_auth(app):
    from authlib.integrations.httpx_client import AsyncOAuth2Client

    metadata_cache, metadata_expires = None, 0
    metadata_lock = asyncio.Lock()

    async def finish_login(request, user_id, *, button_key=None, button_expires=0):
        from urllib.parse import urlencode

        settings = request.app.state.settings
        try:
            await request.app.state.management.principal(user_id)
        except ManagementError as exc:
            return RedirectResponse("/?" + urlencode({"login_error": str(exc)}))
        async with request.app.state.db.transaction() as session:
            if button_key:
                from sqlalchemy import delete

                await session.execute(delete(LoginAttempt).where(LoginAttempt.expires_at <= now()))
                if await session.get(LoginAttempt, button_key):
                    return RedirectResponse("/?" + urlencode({"login_error": "此登录授权已使用，请重新点击机器人中的登录按钮"}))
                session.add(LoginAttempt(state=button_key, nonce="", verifier="", expires_at=button_expires))
            raw = secrets.token_urlsafe(48)
            session.add(Session(
                id=hashlib.sha256(raw.encode()).hexdigest(), user_id=user_id,
                csrf=secrets.token_urlsafe(32), expires_at=now() + 28800,
            ))
        response = RedirectResponse("/", headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
        response.delete_cookie("purrai_login", path="/auth")
        response.set_cookie(
            "purrai_session", raw, httponly=True, secure=settings.cookie_secure, samesite="lax", max_age=28800,
        )
        return response

    @app.get("/auth/telegram/button")
    async def button_login(request: Request):
        from urllib.parse import urlencode

        settings = request.app.state.settings
        if not settings.bot_token or len(settings.session_secret) < 32:
            raise HTTPException(503, "尚未配置机器人网页登录，请完成 Bot Token 与会话密钥配置")
        try:
            user_id, key, expires = telegram_button_identity(request.query_params, settings.bot_token)
        except (ValueError, KeyError):
            return RedirectResponse("/?" + urlencode({"login_error": "Telegram 授权无效或已过期，请重新点击机器人中的登录按钮"}))
        return await finish_login(request, user_id, button_key=key, button_expires=expires)

    async def discovery():
        import httpx
        nonlocal metadata_cache, metadata_expires

        async with metadata_lock:
            if metadata_cache is None or now() >= metadata_expires:
                async with httpx.AsyncClient(timeout=10) as client:
                    response = await client.get("https://oauth.telegram.org/.well-known/openid-configuration")
                    response.raise_for_status()
                    metadata_cache, metadata_expires = response.json(), now() + 600
            return metadata_cache

    @app.get("/auth/telegram/start")
    async def login_start(request: Request):
        settings = request.app.state.settings
        if not settings.login_ready:
            raise HTTPException(503, "尚未配置 Telegram 登录，请完成 OIDC 与会话密钥配置")
        state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
        try:
            metadata = await discovery()
        except Exception as exc:
            raise HTTPException(503, "暂时无法连接 Telegram 登录服务，请稍后重试") from exc
        async with request.app.state.db.transaction() as session:
            from sqlalchemy import delete, func, select

            await session.execute(delete(LoginAttempt).where(LoginAttempt.expires_at <= now()))
            if await session.scalar(select(func.count()).select_from(LoginAttempt)) >= 1000:
                raise HTTPException(429, "登录请求过多，请稍后重试")
            session.add(LoginAttempt(state=state, nonce=nonce, verifier=verifier, expires_at=now() + 600))
        client = AsyncOAuth2Client(
            settings.oidc_client_id,
            settings.oidc_client_secret,
            scope="openid profile",
            code_challenge_method="S256",
            redirect_uri=settings.public_url.rstrip("/") + "/auth/telegram/callback",
        )
        url, _ = client.create_authorization_url(
            metadata["authorization_endpoint"], state=state, nonce=nonce, code_verifier=verifier
        )
        await client.aclose()
        response = RedirectResponse(url)
        response.set_cookie(
            "purrai_login",
            state,
            httponly=True,
            secure=settings.cookie_secure,
            samesite="lax",
            max_age=600,
            path="/auth",
        )
        return response

    @app.get("/auth/telegram/callback")
    async def login_callback(request: Request, state: str = "", code: str = "", error: str = ""):
        settings = request.app.state.settings
        if (
            not settings.login_ready
            or error
            or not code
            or not state
            or not secrets.compare_digest(state, request.cookies.get("purrai_login", ""))
        ):
            return RedirectResponse("/?login_error=登录已取消或会话已失效")
        async with request.app.state.db.transaction() as session:
            attempt = await session.get(LoginAttempt, state)
            if not attempt or attempt.expires_at <= now():
                return RedirectResponse("/?login_error=登录已过期，请重试")
            verifier, nonce = attempt.verifier, attempt.nonce
            attempt.expires_at = 0  # One-time state; retain inert record rather than silently deleting it.
        try:
            from authlib.jose import jwt
            import httpx

            metadata = await discovery()
            async with AsyncOAuth2Client(
                settings.oidc_client_id,
                settings.oidc_client_secret,
                redirect_uri=settings.public_url.rstrip("/") + "/auth/telegram/callback",
                token_endpoint_auth_method="client_secret_basic",
                timeout=10,
            ) as client:
                token = await client.fetch_token(
                    metadata["token_endpoint"],
                    code=code,
                    code_verifier=verifier,
                    grant_type="authorization_code",
                )
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(metadata["jwks_uri"])
                response.raise_for_status()
                keys = response.json()
            claims = jwt.decode(
                token["id_token"],
                keys,
                claims_options={
                    "iss": {"essential": True, "value": "https://oauth.telegram.org"},
                    "aud": {"essential": True, "value": settings.oidc_client_id},
                    "exp": {"essential": True},
                    "nonce": {"essential": True, "value": nonce},
                    "id": {"essential": True},
                },
            )
            claims.validate(leeway=30)
            user_id = int(claims["id"])
        except Exception:
            return RedirectResponse("/?login_error=Telegram身份核验失败，请重新登录")
        return await finish_login(request, user_id)

    @app.post("/auth/logout")
    async def logout(request: Request, user: Identity = Depends(identity)):
        digest = hashlib.sha256(request.cookies.get("purrai_session", "").encode()).hexdigest()
        async with request.app.state.db.transaction() as session:
            login = await session.get(Session, digest)
            if login:
                login.expires_at = 0
        response = RedirectResponse("/", status_code=303)
        response.delete_cookie("purrai_session")
        return response
