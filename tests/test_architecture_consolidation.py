"""Shared record choices, delivery labels and isolated deletion projections."""

from types import SimpleNamespace

import pytest
from sqlalchemy import select

from purrai.action_queue import enqueue
from purrai.admin_labels import OPERATIONS, STATE
from purrai.archive import record_deletion
from purrai.models import ChatMessage, Verification, Violation, now
from purrai.notification_actions import NotificationActions
from purrai.runtime import Runtime
from test_bot_admin import markup
from test_verification import fixed_challenge


async def assert_bot_operations(runtime, telegram, kind, identifier, expected):
    await runtime.admin.detail(1, -1001, kind, identifier)
    labels = [button["text"] for row in markup(telegram)["reply_markup"]["inline_keyboard"] for button in row]
    candidates = ("release", "reissue", "ban") if kind == "verification" else ("revoke_warning", "unmute")
    for operation in (*candidates, "unban"):
        assert (OPERATIONS[operation] in labels) == (operation in expected)


@pytest.mark.parametrize("state,failure,expected", [
    ("pending", "", ["release", "reissue", "ban"]),
    ("releasing", "", ["release", "reissue", "ban"]),
    ("failed", "permanent_mute", ["release", "reissue"]),
    ("failed", "permanent_ban", ["unban"]),
    ("failed", "decline", ["unban"]),
    ("failed", "legacy_ban", ["unban"]),
    ("failed", "kick", []),
    ("passed", "", []),
])
async def test_verification_choices_match_http_and_bot(env, client, monkeypatch, state, failure, expected):
    row = await fixed_challenge(env, monkeypatch)
    _, db, telegram, service = env
    async with db.transaction() as session:
        verification = await session.get(Verification, row.id)
        verification.state, verification.failure_action = state, failure
    response = await client.get("/api/groups/-1001/verifications")
    assert response.status_code == 200
    assert response.json()["items"][0]["allowed_operations"] == expected
    await assert_bot_operations(Runtime(db, telegram, service), telegram, "verification", row.id, expected)


@pytest.mark.parametrize("flags,penalty,expected", [
    ({"profile_violation": True}, None, ["unmute"]),
    ({"rule_mute": True}, None, ["unmute"]),
    ({"warning_active": True}, None, ["revoke_warning", "unmute", "unban"]),
    ({"warning_active": True}, "contact_ban", ["revoke_warning", "unban"]),
    ({}, "advertisement_ban", ["unban"]),
    ({"sender_chat": True, "profile_violation": True}, None, []),
])
async def test_violation_choices_match_http_and_bot(env, client, flags, penalty, expected):
    _, db, telegram, service = env
    async with db.transaction() as session:
        row = Violation(group_id=-1001, user_id=42, message_id=87,
                        warning_active=False, **{key: value for key, value in flags.items() if key != "warning_active"})
        row.warning_active = flags.get("warning_active", False)
        session.add(row)
        await session.flush()
        identifier = row.id
        if penalty:
            await enqueue(session, penalty, "record-choice-penalty", -1001, 42, {"violation_id": row.id})
    response = await client.get("/api/groups/-1001/violations")
    assert response.status_code == 200
    assert response.json()["items"][0]["allowed_operations"] == expected
    await assert_bot_operations(Runtime(db, telegram, service), telegram, "violation", identifier, expected)


@pytest.mark.parametrize("kind,user_id", [("join_link", None), ("archive_export", None), ("manual_mute", 42)])
async def test_notifications_use_public_labels_without_a_missing_user(kind, user_id):
    source = SimpleNamespace(kind=kind, user_id=user_id, state="succeeded", error="", payload={"invite_link": "link"})

    class Session:
        async def get(self, model, identifier):
            return source

    sent = []

    async def call(session, action, method, **kwargs):
        kwargs.pop("before_send")()
        sent.append(kwargs["text"])
        return {"message_id": 123}

    worker = NotificationActions(None, SimpleNamespace(call=call))
    action = SimpleNamespace(payload={"action_ids": ["source"], "chat_id": 1})
    await worker.notify(Session(), action, SimpleNamespace(title="测试群"), None, None)
    assert OPERATIONS[kind] in sent[0] and STATE["succeeded"] in sent[0]
    assert "None" not in sent[0]
    assert ("用户 42" in sent[0]) == (user_id == 42)


async def test_deletion_queue_invalidates_chat_without_an_archive_record(env):
    _, db, _, _ = env
    async with db.transaction() as session:
        message = ChatMessage(group_id=-1001, topic_id=0, message_id=98, sender_id=42,
                              sender_name="成员", text="历史上下文", sent_at=now(), eligible=True)
        session.add(message)
        await session.flush()
        action = SimpleNamespace(kind="delete", group_id=-1001, id="archive-only", state="pending",
                                 error="", payload={"message_id": 98})
        await record_deletion(session, action)
        assert message.eligible  # Archive projection owns no chat writes.
        await enqueue(session, "delete", "chat-without-archive", -1001, 42, {"message_id": 98})
    async with db.sessions() as session:
        message = await session.scalar(select(ChatMessage).where(ChatMessage.message_id == 98))
        assert not message.eligible
