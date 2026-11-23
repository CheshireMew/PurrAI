"""Explicit localhost-only development app. Never loaded by the production CLI."""
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from purrai.app import create_app
from purrai.archive import capture
from purrai.auth import Identity, identity
from purrai.config import Settings
from purrai.db import Database
from purrai.lottery_schemas import LotteryConfig
from purrai.models import Group, Lottery, Member, Meta, PointsAccount, PointsPolicy, now
from purrai.points_policy import default_policy
from purrai.telegram import ALL_PERMISSIONS

ROOT = Path(__file__).resolve().parents[1]
LOCAL_ACTOR = 1


class LocalTelegram:
    """Exercise real application actions without connecting to any real bot."""
    bot = None
    user_id = 999000
    username = "PurrAILocalBot"
    display_name = "PurrAI Local"
    connected = True

    def __init__(self, db):
        self.db, self.message_id = db, 1_000_000
        self.defaults = dict.fromkeys(ALL_PERMISSIONS, True)

    async def call(self, method, **kwargs):
        chat_id, user_id = kwargs.get("chat_id"), kwargs.get("user_id")
        if method == "get_chat_administrators":
            return [{"status": "creator", "user": {"id": LOCAL_ACTOR, "is_bot": False, "first_name": "Local"}}]
        if method == "get_chat_member":
            if user_id in (LOCAL_ACTOR, self.user_id):
                return {"status": "administrator", "can_delete_messages": True, "can_restrict_members": True,
                        "can_invite_users": True, "user": {"id": user_id, "is_bot": user_id == self.user_id, "first_name": "Local"}}
            async with self.db.sessions() as session:
                row = await session.get(Meta, f"local-telegram:{chat_id}:{user_id}")
            return json.loads(row.value) if row else {"status": "member", "user": {"id": user_id, "is_bot": False, "first_name": f"Local {user_id}"}}
        if method == "get_chat":
            async with self.db.sessions() as session:
                group = await session.get(Group, chat_id) if isinstance(chat_id, int) and chat_id < 0 else None
            return {"id": chat_id, "title": group.title if group else "Local", "type": "supergroup" if group else "private",
                    "permissions": self.defaults}
        if method in ("restrict_chat_member", "ban_chat_member", "unban_chat_member"):
            status = {"status": "restricted", "is_member": True, "until_date": kwargs.get("until_date", 0), **kwargs.get("permissions", {})}
            if method == "ban_chat_member":
                status = {"status": "kicked", "until_date": kwargs.get("until_date", 0)}
            elif method == "unban_chat_member":
                status = {"status": "left"}
            async with self.db.transaction() as session:
                key = f"local-telegram:{chat_id}:{user_id}"
                row = await session.get(Meta, key)
                if row is None:
                    row = Meta(key=key)
                    session.add(row)
                row.value = json.dumps(status)
            return True
        if method.startswith(("send_", "edit_message_")):
            self.message_id += 1
            return {"message_id": kwargs.get("message_id", self.message_id)}
        if method in ("delete_message", "answer_callback_query", "pin_chat_message", "unpin_chat_message",
                      "approve_chat_join_request", "decline_chat_join_request", "set_chat_permissions"):
            return True
        if method == "create_chat_invite_link":
            return {"invite_link": "https://t.me/+LocalTestingOnly", "creates_join_request": True}
        raise RuntimeError(f"Local development does not simulate Telegram method: {method}")

    async def close(self):
        pass


async def seed_demo(db):
    data = json.loads((ROOT / "scripts" / "local_demo.json").read_text(encoding="utf-8"))
    async with db.transaction() as session:
        if await session.get(Meta, "local-dev:seed:v1"):
            return
        from purrai.shared_policy import initial_settings
        group = Group(**data["group"], type="supergroup", approved=True, enabled=True, status="active",
                      settings=await initial_settings(session))
        session.add(group)
        await session.flush()
        policy = default_policy().model_dump()
        policy["enabled"] = True
        session.add(PointsPolicy(group_id=group.id, config=policy))
        for item in data["accounts"]:
            session.add(PointsAccount(group_id=group.id, source_key=f"local-demo:{item['user_id']}", **item))
            session.add(Member(group_id=group.id, user_id=item["user_id"], joined_at=now() - 30 * 86400,
                               passed_at=now() - 30 * 86400, observed_messages=10))
        session.add(Lottery(group_id=group.id, created_by=LOCAL_ACTOR, config=LotteryConfig.model_validate(data["lottery"]).model_dump()))
        await session.flush()
        for number, item in enumerate(data["messages"], 1):
            await capture(session, {"chat": {"id": group.id, "type": "supergroup"}, "message_id": number,
                "date": int(now()), "from": {"id": item["user_id"], "is_bot": False, "first_name": item["display_name"]},
                "text": item["text"]})
        session.add(Meta(key="local-dev:seed:v1", value="1"))


def create_local_app():
    database_file = Path(os.environ.get("PURRAI_DEV_DATABASE", ROOT / "data" / "local-dev" / "purrai.db")).resolve()
    settings = Settings(_env_file=None, database_url=f"sqlite+aiosqlite:///{database_file.as_posix()}",
        bot_token="", chat_api_key="", oidc_client_id="", oidc_client_secret="", superadmin_ids=[LOCAL_ACTOR],
        cookie_secure=False, backup_dir=database_file.parent / "backups")
    db = Database(settings.database_url)
    app = create_app(settings, db, LocalTelegram(db), start_runtime=False)
    app.state.local_dev = True

    async def local_identity(request: Request):
        if request.client.host not in ("127.0.0.1", "::1"):
            raise HTTPException(403, "Local development is available only on this computer")
        return Identity(id=LOCAL_ACTOR, root=True, csrf="local-development")

    app.dependency_overrides[identity] = local_identity

    @app.middleware("http")
    async def localhost_only(request, call_next):
        if request.client.host not in ("127.0.0.1", "::1") or request.url.hostname not in ("127.0.0.1", "localhost", "::1"):
            return JSONResponse({"detail": "Local development is available only on this computer"}, status_code=403)
        return await call_next(request)

    @app.get("/__local/status")
    async def local_status():
        return {"mode": "local-development", "project": str(ROOT), "pid": os.getpid()}

    # The normal app's final SPA fallback must remain after this development route.
    app.router.routes.insert(0, app.router.routes.pop())

    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        try:
            async with original_lifespan(application):
                await seed_demo(db)
                service = app.state.service
                app.state.runtime.workers.start([("local-actions", service.action_loop), ("local-cleanup", service.cleanup_loop),
                                                ("local-timeouts", service.timeout_loop)])
                yield
        finally:
            await db.close()

    app.router.lifespan_context = lifespan
    return app
