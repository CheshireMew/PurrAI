"""Confirmed source deletion retracts every known reply, including partial sends."""

import json

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.methods import DeleteMessage, SendSticker
from sqlalchemy import delete, select, update

from conftest import message
from purrai.action_queue import enqueue
from purrai.deletion_effects import deletion_result
from purrai.chat import GroupChat
from purrai.chat_model import ChatReply
from purrai.models import Action, ChatMessage, ChatTurn, Group
from test_chat import configured, due
from test_stickers import cache


async def confirm_absent(chat, identifier=1):
    async with chat.db.transaction() as session:
        action = await enqueue(session, "delete", f"external-confirmed:{identifier}", -1001,
                               payload={"message_id": identifier, "already_absent": True})
        action.state = "succeeded"
        await deletion_result(session, action)


async def cleanup(service):
    while await service.run_one_cleanup():
        pass


@pytest.mark.parametrize("archive_enabled", [False, True])
async def test_text_sent_before_missing_sticker_target_is_retracted(env, archive_enabled):
    chat, model = await configured(env, archive_enabled=archive_enabled)
    await cache(chat)
    model.result = ChatReply("已经发出的文字", "😸")
    original = chat.tg.call

    async def telegram(method, **kwargs):
        if method == "send_sticker":
            raise TelegramBadRequest(method=SendSticker(**kwargs), message="message to be replied not found")
        return await original(method, **kwargs)

    chat.tg.call = telegram
    await chat.observe(message(1, "柴郡喵，聊聊周末"))
    assert await chat.run_one()
    async with chat.db.sessions() as session:
        turn = await session.scalar(select(ChatTurn))
        assert turn.state == "retracting" and turn.sent_message_id == 101
    await cleanup(env[3])
    assert [kwargs["message_id"] for method, kwargs in chat.tg.calls if method == "delete_message"] == [101]
    async with chat.db.sessions() as session:
        assert (await session.scalar(select(ChatTurn))).state == "retracted"


@pytest.mark.parametrize("archive_enabled", [False, True])
async def test_source_deletion_after_a_complete_reply_removes_text_and_sticker_after_restart(env, archive_enabled):
    chat, model = await configured(env, archive_enabled=archive_enabled)
    await cache(chat)
    model.result = ChatReply("先出去走走吧。", "😸")
    await chat.observe(message(1, "柴郡喵，周末去哪玩？"))
    await chat.run_one()
    async with chat.db.transaction() as session:
        turn = await session.scalar(select(ChatTurn))
        assert json.loads(turn.response)["sent_message_ids"] == [101, 102]
        # Replies remain recoverable after the rolling context is pruned and
        # chat/protection is disabled. Only confirmed source deletion triggers it.
        await session.execute(delete(ChatMessage))
        group = await session.get(Group, -1001)
        group.enabled = False
        group.settings = {**group.settings, "chat_enabled": False}
    await confirm_absent(chat)
    replacement = GroupChat(chat.db, chat.tg, chat.settings, model=model)
    await replacement.recover()
    await confirm_absent(chat)
    await cleanup(env[3])
    assert {kwargs["message_id"] for method, kwargs in chat.tg.calls if method == "delete_message"} == {101, 102}
    async with chat.db.sessions() as session:
        assert (await session.scalar(select(ChatTurn))).state == "retracted"
        assert len((await session.scalars(select(Action).where(Action.dedupe_key.startswith("cleanup:chat-reply:")))).all()) == 2


async def test_moderation_delete_success_triggers_retraction_without_waiting_for_a_missing_reply_error(env):
    chat, _ = await configured(env)
    await chat.observe(message(1, "柴郡喵，聊聊周末"))
    await chat.run_one()
    async with chat.db.transaction() as session:
        await enqueue(session, "delete", "moderation-delete", -1001, payload={"message_id": 1})
    assert await env[3].run_one_action(include_progress=False, include_cleanup=False)
    await cleanup(env[3])
    assert [kwargs["message_id"] for method, kwargs in chat.tg.calls if method == "delete_message"] == [1, 101]


async def test_source_disappearing_before_text_receipt_is_saved_still_retracts_it(env):
    chat, _ = await configured(env)
    original = chat.tg.call

    async def telegram(method, **kwargs):
        result = await original(method, **kwargs)
        if method == "send_message":
            await confirm_absent(chat)
        return result

    chat.tg.call = telegram
    await chat.observe(message(1, "柴郡喵，聊聊周末"))
    await chat.run_one()
    await cleanup(env[3])
    assert any(method == "delete_message" and kwargs["message_id"] == 101 for method, kwargs in chat.tg.calls)
    async with chat.db.sessions() as session:
        assert (await session.scalar(select(ChatTurn))).state == "retracted"


async def test_source_disappearing_before_sticker_receipt_is_saved_retracts_both(env):
    chat, model = await configured(env)
    await cache(chat)
    model.result = ChatReply("先出去走走吧。", "😸")
    original = chat.tg.call

    async def telegram(method, **kwargs):
        result = await original(method, **kwargs)
        if method == "send_sticker":
            await confirm_absent(chat)
        return result

    chat.tg.call = telegram
    await chat.observe(message(1, "柴郡喵，聊聊周末"))
    await chat.run_one()
    await cleanup(env[3])
    assert {kwargs["message_id"] for method, kwargs in chat.tg.calls if method == "delete_message"} == {101, 102}


async def test_cleanup_rate_limit_survives_restart_without_resending_text(env):
    chat, _ = await configured(env)
    await chat.observe(message(1, "柴郡喵，聊聊周末"))
    await chat.run_one()
    await confirm_absent(chat)
    original = chat.tg.call
    attempts = []

    async def telegram(method, **kwargs):
        if method == "delete_message" and not attempts:
            attempts.append(kwargs)
            raise TelegramRetryAfter(method=DeleteMessage(**kwargs), message="retry", retry_after=2)
        return await original(method, **kwargs)

    chat.tg.call = telegram
    assert await env[3].run_one_cleanup()
    await GroupChat(chat.db, chat.tg, chat.settings, model=chat.model).recover()
    async with chat.db.transaction() as session:
        await session.execute(update(Action).where(Action.state == "pending").values(available_at=0))
    await cleanup(env[3])
    assert len([method for method, _ in chat.tg.calls if method == "send_message"]) == 1
    async with chat.db.sessions() as session:
        assert (await session.scalar(select(ChatTurn))).state == "retracted"


async def test_legacy_partial_cache_cannot_bypass_the_new_advertising_verdict(env):
    chat, model = await configured(env)
    await cache(chat)
    model.result = ChatReply("此前已发出的文字", "😸")
    original = chat.tg.call
    attempts = []

    async def telegram(method, **kwargs):
        if method == "send_sticker":
            attempts.append(kwargs)
            raise TelegramRetryAfter(method=SendSticker(**kwargs), message="retry", retry_after=2)
        return await original(method, **kwargs)

    chat.tg.call = telegram
    await chat.observe(message(1, "柴郡喵，聊聊周末"))
    await chat.run_one()
    async with chat.db.transaction() as session:
        turn = await session.scalar(select(ChatTurn))
        saved = json.loads(turn.response)
        saved.pop("is_advertisement")
        turn.response = json.dumps(saved)
    await due(chat)
    await chat.run_one()
    assert len(model.calls) == 1 and len(attempts) == 1
    assert len([method for method, _ in chat.tg.calls if method == "send_message"]) == 1
    await confirm_absent(chat)
    await cleanup(env[3])
    async with chat.db.sessions() as session:
        assert (await session.scalar(select(ChatTurn))).state == "retracted"


async def test_failed_retraction_is_recorded_and_never_reported_as_success(env):
    chat, _ = await configured(env)
    await chat.observe(message(1, "柴郡喵，聊聊周末"))
    await chat.run_one()
    await confirm_absent(chat)
    original = chat.tg.call

    async def telegram(method, **kwargs):
        if method == "delete_message":
            raise TelegramBadRequest(method=DeleteMessage(**kwargs), message="message can't be deleted")
        return await original(method, **kwargs)

    chat.tg.call = telegram
    await cleanup(env[3])
    async with chat.db.sessions() as session:
        assert (await session.scalar(select(ChatTurn))).state == "retraction_failed"


async def test_pending_or_cancelled_deletion_does_not_retract_a_completed_answer(env):
    chat, _ = await configured(env)
    await chat.observe(message(1, "柴郡喵，聊聊周末"))
    await chat.run_one()
    async with chat.db.transaction() as session:
        action = await enqueue(session, "delete", "cancelled-source", -1001, payload={"message_id": 1})
        action.state = "cancelled"
        await deletion_result(session, action)
    await chat.recover()
    assert not await env[3].run_one_cleanup()


async def test_restart_repairs_a_legacy_skipped_turn_that_already_has_a_text_receipt(env):
    chat, _ = await configured(env)
    await chat.observe(message(1, "柴郡喵，聊聊周末"))
    await chat.run_one()
    async with chat.db.transaction() as session:
        turn = await session.scalar(select(ChatTurn))
        turn.state, turn.response = "skipped", ""
        # This is the old release's durable state after a sticker send discovered
        # the missing target. No new Telegram send is needed to reconstruct it.
        session.add(Action(dedupe_key="legacy-absence", group_id=-1001, kind="delete", state="succeeded",
                           payload={"message_id": 1, "already_absent": True}))
    await chat.recover()
    await cleanup(env[3])
    assert any(method == "delete_message" and kwargs["message_id"] == 101 for method, kwargs in chat.tg.calls)


async def test_deletion_fact_for_a_different_chat_does_not_retract_the_reply(env):
    chat, _ = await configured(env)
    await chat.observe(message(1, "柴郡喵，聊聊周末"))
    await chat.run_one()
    async with chat.db.transaction() as session:
        action = await enqueue(session, "delete", "different-chat", -1001,
                               payload={"message_id": 1, "chat_id": -1009, "already_absent": True})
        action.state = "succeeded"
        await deletion_result(session, action)
    await chat.recover()
    assert not await env[3].run_one_cleanup()


async def test_deletion_of_a_referenced_channel_post_retracts_the_answer(env):
    from test_chat_explicit_media import channel_post

    chat, _ = await configured(env)
    await chat.observe(message(1, "柴郡喵，怎么看？", reply_to_message=channel_post()))
    await chat.run_one()
    await confirm_absent(chat, 700)
    await cleanup(env[3])
    assert any(method == "delete_message" and kwargs["message_id"] == 101 for method, kwargs in chat.tg.calls)
