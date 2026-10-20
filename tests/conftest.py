import hashlib
import time

import pytest
from aiogram import Bot
from httpx import ASGITransport, AsyncClient

from purrai.app import create_app
from purrai.config import Settings
from purrai.db import Database, schema_head
from purrai.models import Base, Group, Session, default_settings
from purrai.service import Service
from purrai.telegram import ALL_PERMISSIONS


class FakeTelegram:
    def __init__(self):
        self.user_id, self.connected = 999, True
        self.bot = Bot("999:TEST_TOKEN_FOR_OFFLINE_TESTS_ONLY")
        self.calls, self.members = [], {}
        self.fail = {}
        self.message_id = 100
        self.defaults = dict.fromkeys(ALL_PERMISSIONS, True)

    async def call(self, method, **kwargs):
        self.calls.append((method, kwargs))
        if self.fail.get(method):
            self.fail[method] -= 1
            raise ConnectionError("offline")
        key = (kwargs.get("chat_id"), kwargs.get("user_id"))
        if method == "get_chat_member":
            if kwargs["user_id"] == self.user_id:
                return {
                    "status": "administrator",
                    "can_delete_messages": True,
                    "can_restrict_members": True,
                    "can_invite_users": True,
                }
            return self.members.get(key, {"status": "member", "user": {"id": kwargs["user_id"]}})
        if method == "get_chat":
            return {"permissions": self.defaults}
        if method == "restrict_chat_member":
            self.members[key] = {
                "status": "restricted",
                "is_member": True,
                "until_date": kwargs.get("until_date", 0),
                **kwargs["permissions"],
            }
        if method == "ban_chat_member":
            self.members[key] = {"status": "kicked", "until_date": kwargs["until_date"]}
        if method == "unban_chat_member":
            self.members[key] = {"status": "left"}
        if method == "approve_chat_join_request":
            self.members[key] = {"status": "member"}
        if method == "decline_chat_join_request":
            self.members[key] = {"status": "left"}
        if method == "create_chat_invite_link":
            return {"invite_link": "https://t.me/+offline-test", "creates_join_request": True}
        if method in ("send_photo", "send_message", "send_sticker"):
            self.message_id += 1
            return {"message_id": self.message_id}
        if method == "get_updates":
            return []
        if method == "get_webhook_info":
            return {"url": ""}
        return True

    async def close(self):
        await self.bot.session.close()


@pytest.fixture
async def env(tmp_path):
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'db.sqlite3'}",
        superadmin_ids=[1],
        backup_dir=tmp_path / "backups",
        chat_user_interval=0, chat_group_interval=0,
        chat_daily_tokens=100000000, chat_group_daily_tokens=100000000, chat_user_daily_tokens=100000000,
    )
    database = Database(settings.database_url)
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        from sqlalchemy import text
        await connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)"))
        await connection.execute(text("INSERT INTO alembic_version VALUES (:revision)"), {"revision": schema_head()})
    async with database.transaction() as session:
        session.add(
            Group(
                approved=True,
                id=-1001,
                title="测试群",
                type="supergroup",
                settings=default_settings(),
                enabled=True,
                status="active",
                activated_at=time.time() - 60,
            )
        )
    telegram = FakeTelegram()
    service = Service(database, telegram, settings)
    yield settings, database, telegram, service
    await telegram.close()
    await database.close()


@pytest.fixture
async def client(env):
    settings, database, telegram, _ = env
    app = create_app(settings, database, telegram, start_runtime=False)
    async with database.transaction() as session:
        session.add(
            Session(
                id=hashlib.sha256(b"test-session").hexdigest(),
                user_id=1,
                csrf="test-csrf",
                expires_at=time.time() + 3600,
            )
        )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        cookies={"purrai_session": "test-session"},
        headers={"X-CSRF-Token": "test-csrf"},
    ) as client:
        yield client


def join(user_id=42, date=None, actor=1, old="left", new="member"):
    return {
        "chat": {"id": -1001, "type": "supergroup", "title": "测试群"},
        "date": int(date or time.time()),
        "from": {"id": actor},
        "old_chat_member": {"status": old, "user": {"id": user_id, "is_bot": False, "first_name": "测试"}},
        "new_chat_member": {"status": new, "user": {"id": user_id, "is_bot": False, "first_name": "测试"}},
    }


def message(identifier, text, user_id=42, **kwargs):
    return {
        "message_id": identifier,
        "date": int(time.time()),
        "chat": {"id": -1001, "type": "supergroup"},
        "from": {"id": user_id},
        "text": text,
        **kwargs,
    }


async def drain(service, count=30):
    for _ in range(count):
        if not await service.run_one_action():
            break
