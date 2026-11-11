"""Exercise saved keyword replies through management and the real event/worker path."""

from unittest.mock import AsyncMock

import pytest
from aiogram.types import Message
from pydantic import ValidationError
from sqlalchemy import select

from conftest import drain, message
from purrai.action_queue import enqueue
from purrai.auto_replies import AutoReplies
from purrai.auto_reply_schemas import AutoReplyInput, AutoReplyPreview
from purrai.management import Management, ManagementError
from purrai.models import Action, AutoReply, Group, Verification, now, uid
from purrai.runtime import Runtime


def outgoing(telegram):
    return [data for method, data in telegram.calls if method == "send_message" and data["chat_id"] == -1001]


async def save(env, keyword=None, **values):
    settings, database, telegram, _ = env
    management = Management(database, telegram, settings=settings)
    row = await management.auto_replies.save(1, -1001, AutoReplyInput(
        **{"keyword": keyword or uid(), "reply": "第一行\n第二行 <内容>", "cooldown_seconds": 0, **values}))
    return management, row


async def test_arbitrary_saved_keyword_edits_and_disable_take_effect_immediately(env):
    _, database, telegram, service = env
    management, row = await save(env)
    replies = AutoReplies(database)
    assert await replies.observe(message(10, row["keyword"]))
    await drain(service)
    assert outgoing(telegram)[-1]["text"] == row["reply"]
    replacement = uid()
    row = await management.auto_replies.save(1, -1001, AutoReplyInput(keyword=replacement, reply="已修改", cooldown_seconds=0),
        row["id"], expected_version=row["version"])
    assert not await replies.observe(message(11, row["keyword"] + "额外文字"))
    assert not await replies.observe(message(12, "旧词不会被保留"))
    # Verify the actual arbitrary original value has ceased matching.
    async with database.sessions() as session:
        original = await session.scalar(select(Action).where(Action.kind == "auto_reply_rule").order_by(Action.created_at))
        old_keyword = original.payload["result"]["keyword"]
    assert not await replies.observe(message(13, old_keyword))
    assert await replies.observe(message(14, replacement))
    await drain(service)
    assert outgoing(telegram)[-1]["text"] == "已修改"
    await management.auto_replies.save(1, -1001, None, row["id"], expected_version=row["version"], changes={"enabled": False})
    assert not await replies.observe(message(15, replacement))
    assert len(outgoing(telegram)) == 2


async def test_api_idempotency_concurrency_and_group_permissions(client, env):
    body = {"keyword": uid(), "reply": "可维护回复"}
    path = "/api/groups/-1001/auto-replies"
    response = await client.post(path, json=body, headers={"Idempotency-Key": "create-auto-reply"})
    assert response.status_code == 200, response.text
    row = response.json()
    assert (await client.post(path, json=body, headers={"Idempotency-Key": "create-auto-reply"})).json() == row
    assert (await client.get(path)).json()["total"] == 1
    assert (await client.post(path + "/preview", json={"text": body["keyword"]})).json()["rule"]["reply"] == body["reply"]
    headers = {"If-Match": row["version"], "Idempotency-Key": "update-auto-reply"}
    response = await client.put(path + "/" + row["id"], json={**body, "reply": "新回复"}, headers=headers)
    assert response.status_code == 200, response.text
    response = await client.put(path + "/" + row["id"], json={**body, "reply": "过时修改"},
        headers={**headers, "Idempotency-Key": "stale-auto-reply"})
    assert response.status_code == 409
    settings, database, telegram, _ = env
    manager = Management(database, telegram, settings=settings)
    with pytest.raises(ManagementError) as error:
        await manager.auto_replies.page(7, -1001)
    assert error.value.status == 403
    async with database.transaction() as session:
        session.add(Group(id=-1002, approved=True))
    response = await client.put("/api/groups/-1002/auto-replies/" + row["id"], json=body,
        headers={**headers, "Idempotency-Key": "other-group-edit"})
    assert response.status_code == 404
    assert (await client.post(path, json={"keyword": " ", "reply": "x"},
        headers={"Idempotency-Key": "empty-keyword-test"})).status_code == 422


async def test_matching_priority_cooldown_and_preview(env):
    _, database, telegram, service = env
    manager, first = await save(env, "ＡbC", priority=1, mode="contains", cooldown_seconds=60, reply="首选")
    await save(env, "abc", priority=0, mode="contains", reply="后选")
    result = await manager.auto_replies.preview(1, -1001, AutoReplyPreview(text="前缀 aBc 后缀"))
    assert result["rule"]["id"] == first["id"]
    replies = AutoReplies(database)
    assert await replies.observe(message(20, "前缀 abc 后缀"))
    assert await replies.observe(message(21, "ABC", 43))
    await drain(service)
    assert [item["text"] for item in outgoing(telegram)] == ["首选"]
    # Replaying the cooldown-suppressed message after the window cannot send it later.
    async with database.transaction() as session:
        (await session.get(AutoReply, first["id"])).last_triggered_at = 0
    assert await AutoReplies(database).observe(message(21, "ABC", 43))
    assert await replies.observe(message(22, "ABC"))
    await drain(service)
    assert len(outgoing(telegram)) == 2


async def test_event_ingress_suppresses_ai_and_sends_in_source_topic_once(env):
    _, database, telegram, service = env
    _, row = await save(env)
    runtime = Runtime(database, telegram, service)
    runtime.events.chat = type("ChatSpy", (), {"observe": AsyncMock()})()
    incoming = message(30, row["keyword"], message_thread_id=17, is_topic_message=True)
    incoming["from"].update(is_bot=False, first_name="群友")
    for _ in range(2):
        await runtime.events.message(Message.model_validate(incoming))
    await drain(service)
    runtime.events.chat.observe.assert_not_awaited()
    sent = outgoing(telegram)
    assert len(sent) == 1
    assert sent[0]["message_thread_id"] == 17
    assert sent[0]["reply_parameters"]["message_id"] == 30
    assert sent[0]["parse_mode"] is None
    await runtime.admin.ui.close()


@pytest.mark.parametrize("change", ["disable", "edit_rule", "edit_message", "verification", "deleted", "revoke", "expired"])
async def test_pending_response_rechecks_latest_state(env, change):
    _, database, telegram, service = env
    manager, row = await save(env)
    replies = AutoReplies(database)
    incoming = message(40, row["keyword"])
    assert await replies.observe(incoming)
    if change in ("disable", "edit_rule"):
        await manager.auto_replies.save(1, -1001, None, row["id"], expected_version=row["version"],
            changes={"enabled": False} if change == "disable" else {"reply": "替换后的回复"})
    elif change == "edit_message":
        assert await replies.observe({**incoming, "text": "改掉触发词"}, edited=True)
    else:
        async with database.transaction() as session:
            if change == "verification":
                session.add(Verification(group_id=-1001, user_id=42, generation="test", state="pending", deadline=now() + 60))
            elif change == "deleted":
                await enqueue(session, "delete", "delete-source", -1001, 42, {"message_id": 40})
            elif change == "revoke":
                (await session.get(Group, -1001)).approved = False
            else:
                action = await session.scalar(select(Action).where(Action.kind == "auto_reply"))
                action.payload = {**action.payload, "expires_at": now() - 1}
    await drain(service)
    assert not outgoing(telegram)


async def test_waits_for_advertising_check_and_stops_if_deleted(env):
    _, database, telegram, service = env
    _, row = await save(env)
    async with database.transaction() as session:
        check = await enqueue(session, "advertisement_check", "advertising-check:-1001:50", -1001, 42)
    assert await AutoReplies(database).observe(message(50, row["keyword"]))
    await service.run_one_action(lane="notification")
    assert not outgoing(telegram)
    async with database.transaction() as session:
        (await session.get(Action, check.id)).state = "succeeded"
        await enqueue(session, "delete", "blocked-after-check", -1001, 42, {"message_id": 50})
        action = await session.scalar(select(Action).where(Action.kind == "auto_reply"))
        action.available_at = 0
    await drain(service)
    assert not outgoing(telegram)


async def test_bots_commands_edits_other_groups_and_old_messages_do_not_trigger(env):
    _, database, telegram, service = env
    _, row = await save(env)
    replies = AutoReplies(database)
    base = message(60, row["keyword"])
    for incoming in [
        {**base, "from": {"id": 9, "is_bot": True}},
        {**base, "sender_chat": {"id": -9999, "type": "channel"}},
        {**base, "via_bot": {"id": 9}},
        {**base, "text": "/" + row["keyword"]},
        {**base, "date": now() - 121},
        {**base, "chat": {"id": 42, "type": "private"}},
        {**base, "chat": {"id": -1002, "type": "supergroup"}},
    ]:
        assert not await replies.observe(incoming)
    assert not await replies.observe(base, edited=True)
    async with database.transaction() as session:
        (await session.get(Group, -1001)).enabled = False
    assert await replies.observe(base)  # Independent of moderation and AI switches.
    await drain(service)
    assert len(outgoing(telegram)) == 1


async def test_uncertain_send_is_not_duplicated_on_replay(env):
    _, database, telegram, service = env
    _, row = await save(env)
    incoming = message(70, row["keyword"])
    assert await AutoReplies(database).observe(incoming)
    telegram.fail["send_message"] = 1
    await drain(service)
    assert await AutoReplies(database).observe(incoming)
    await drain(service)
    assert len(outgoing(telegram)) == 1
    async with database.sessions() as session:
        action = await session.scalar(select(Action).where(Action.kind == "auto_reply"))
        assert action.state == "failed"
        assert action.payload["delivery"]["state"] == "uncertain"


async def test_bot_private_panel_create_edit_test_and_disable(env):
    from test_bot_admin import click, deliver, open_group, private, texts
    _, database, telegram, service = env
    runtime = Runtime(database, telegram, service)
    await open_group(runtime, telegram)
    await click(runtime, telegram, "关键词自动回复")
    await click(runtime, telegram, "添加自动回复")
    keyword = uid()
    await deliver(runtime, {"message": private(keyword)})
    await deliver(runtime, {"message": private("设置的回复\n支持换行")})
    assert "设置的回复" in texts(telegram)[-1]
    await click(runtime, telegram, "修改关键词")
    replacement = uid()
    await deliver(runtime, {"message": private(replacement)})
    await click(runtime, telegram, "返回自动回复")
    await click(runtime, telegram, "测试自动回复")
    await deliver(runtime, {"message": private(replacement)})
    assert "设置的回复\n支持换行" in texts(telegram)
    await click(runtime, telegram, replacement[:32])
    await click(runtime, telegram, "停用")
    assert not await AutoReplies(database).observe(message(80, replacement))
    await runtime.admin.ui.close()


def test_long_emoji_reply_is_validated_in_telegram_character_units():
    assert AutoReplyInput(keyword="x", reply="😀" * 2000).reply
    with pytest.raises(ValidationError):
        AutoReplyInput(keyword="x", reply="😀" * 2001)
