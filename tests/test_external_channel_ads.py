"""External channel origins are deterministic ad evidence, before any AI call."""

import asyncio
import json

import pytest
from sqlalchemy import func, select

from conftest import drain, message
from purrai.archive import capture
from purrai.chat_state import reply_snapshot, session_state, store_state
from purrai.models import Action, ChatMessage, ChatTurn, Group, MessageRevision, Rule, Violation, Whitelist, now
from purrai.telegram_sources import channel_sources, external_channel_reasons
from test_advertisement_bans import setup
from test_chat import age_reply, configured, due
from test_chat_explicit_media import channel_post

CHANNEL = {"id": -1001942022995, "type": "channel", "title": "频道", "username": "Bitget_crypto1"}


def origin(kind="reference"):
    source = {"type": "channel", "date": 1793023177, "chat": CHANNEL, "message_id": 8227}
    if kind == "reference":
        return {"external_reply": {"origin": source, "chat": CHANNEL, "message_id": 8227,
                                   "photo": [{"file_id": "never-sent-to-model"}]},
                "quote": {"text": "#ETH 下行\n已獲利 +107.09%", "position": 0}}
    if kind == "forward":
        return {"forward_origin": source}
    if kind == "legacy":
        return {"forward_from_chat": CHANNEL, "forward_from_message_id": 8227, "forward_date": 1793023177}
    return {"external_reply": {"origin": {"type": "hidden_user", "sender_user_name": "anonymous"},
                               "chat": CHANNEL, "message_id": 8227}}


@pytest.mark.parametrize("kind", ["reference", "forward", "legacy", "fallback"])
@pytest.mark.parametrize("body", ["我跟着做两周，直接回血80%仓位", "今天就这样吧"])
async def test_channel_origins_delete_and_permanently_ban_even_older_members_without_urls(env, monkeypatch, kind, body):
    await setup(env, monkeypatch)
    _, db, tg, svc = env
    incoming = message(10, body, **origin(kind))
    await svc.message(incoming, update_id=20)
    await svc.message(incoming, update_id=20)
    await drain(svc)
    assert tg.members[(-1001, 42)] == {"status": "kicked", "until_date": 0}
    assert [args["message_id"] for method, args in tg.calls if method == "delete_message"] == [10]
    assert len([1 for method, _ in tg.calls if method == "ban_chat_member"]) == 1
    async with db.sessions() as session:
        violation = await session.scalar(select(Violation))
        assert any("外部频道广告：" in reason and str(CHANNEL["id"]) in reason for reason in violation.reasons)
        assert not violation.warning_active
        revision = await session.scalar(select(MessageRevision))
        assert revision.reasons == violation.reasons
        ban = await session.scalar(select(Action).where(Action.kind == "advertisement_ban"))
        assert ban.state == "succeeded" and ban.payload["ad_evidence"]["message"] == incoming


@pytest.mark.parametrize("kind", ["reference", "forward", "legacy", "fallback"])
@pytest.mark.parametrize("direct", [False, True])
async def test_sources_are_filtered_before_model_even_when_moderation_is_paused(env, kind, direct):
    chat, model = await configured(env)
    async with chat.db.transaction() as session:
        group = await session.get(Group, -1001)
        group.enabled = False
        group.settings = {**group.settings, "external_group_ban": False}
    body = "柴郡喵，我跟着做两周，直接回血80%仓位" if direct else "我跟着做两周，直接回血80%仓位"
    await chat.observe(message(1, body, **origin(kind)))
    await due(chat)
    assert not await chat.run_one() and not model.calls
    assert not (await chat.status(-1001))["commands"]["items"]
    await chat.observe(message(2, "柴郡喵，周末去哪里玩？", user_id=43))
    assert await chat.run_one()
    assert [row["id"] for row in model.calls[0][1]["messages"]] == [2]
    async with chat.db.sessions() as session:
        assert not await session.scalar(select(func.count()).select_from(Violation))
        assert not await session.scalar(select(func.count()).select_from(Action))


@pytest.mark.parametrize("exemption", ["administrator", "whitelist", "superadmin", "allowed_link",
                                      "allowed_post", "allowed_private_post", "allowed_mention", "linked_channel"])
async def test_channel_policy_preserves_existing_trust_and_allowed_sources(env, monkeypatch, exemption):
    original = await setup(env, monkeypatch)
    settings, db, tg, svc = env
    async with db.transaction() as session:
        group = await session.get(Group, -1001)
        if exemption == "whitelist":
            session.add(Whitelist(group_id=-1001, user_id=42, moderation=True))
        elif exemption.startswith("allowed_") and exemption != "allowed_mention":
            link = {"allowed_link": "https://t.me/Bitget_crypto1", "allowed_post": "https://t.me/Bitget_crypto1/8227",
                    "allowed_private_post": "https://t.me/c/1942022995/8227"}[exemption]
            group.settings = {**group.settings, "telegram_allowed_links": [link]}
        elif exemption == "allowed_mention":
            session.add(Rule(group_id=-1001, kind="allow_mention", value="Bitget_crypto1"))
    if exemption == "administrator":
        tg.members[(-1001, 42)] = {"status": "administrator"}
    elif exemption == "superadmin":
        settings.superadmin_ids = [1, 42]
    elif exemption == "linked_channel":
        async def call(method, **kwargs):
            result = await original(method, **kwargs)
            return {**result, "linked_chat_id": CHANNEL["id"]} if method == "get_chat" else result
        monkeypatch.setattr(tg, "call", call)
    await svc.message(message(10, "普通内容", **origin()))
    await drain(svc)
    assert not any(method in ("delete_message", "ban_chat_member", "restrict_chat_member") for method, _ in tg.calls)


@pytest.mark.parametrize("change", ["allowed_source", "switch_off", "whitelist", "administrator"])
async def test_queued_bans_recheck_current_policy_and_exemptions(env, monkeypatch, change):
    await setup(env, monkeypatch)
    _, db, tg, svc = env
    await svc.message(message(10, "普通内容", **origin()))
    async with db.transaction() as session:
        group = await session.get(Group, -1001)
        if change == "allowed_source":
            group.settings = {**group.settings, "telegram_allowed_links": ["https://t.me/Bitget_crypto1"]}
        elif change == "switch_off":
            group.settings = {**group.settings, "external_group_ban": False}
        elif change == "whitelist":
            session.add(Whitelist(group_id=-1001, user_id=42, moderation=True))
    if change == "administrator":
        tg.members[(-1001, 42)] = {"status": "administrator"}
    await drain(svc)
    assert not any(method == "ban_chat_member" for method, _ in tg.calls)


async def test_edit_adding_channel_reference_invalidates_pending_chat_and_keeps_evidence(env, monkeypatch):
    await setup(env, monkeypatch)
    chat, model = await configured(env)
    _, db, _, svc = env
    await chat.observe(message(10, "柴郡喵，今天吃什么？"), update_id=1)
    incoming = message(10, "柴郡喵，今天吃什么？", edit_date=int(now()) + 1, **origin())
    await svc.message(incoming, edited=True, update_id=2)
    await chat.observe(incoming, edited=True, update_id=2)
    assert not await chat.run_one() and not model.calls
    async with db.sessions() as session:
        assert not (await session.scalar(select(ChatMessage))).eligible
        assert (await session.scalar(select(Action).where(Action.kind == "advertisement_ban"))).payload["ad_evidence"]["message"] == incoming


@pytest.mark.parametrize("rolling", [False, True])
async def test_upgrade_filters_archived_channel_origin_and_old_bot_commentary_from_frozen_context(env, rolling):
    chat, model = await configured(env, chat_proactive=False)
    async with chat.db.transaction() as session:
        await capture(session, message(900, "我跟着做两周，直接回血80%仓位", **origin()))
        if rolling:
            session.add(ChatMessage(group_id=-1001, message_id=900, sender_id=42, sender_name="旧用户",
                                    text="我跟着做两周，直接回血80%仓位", sent_at=now()))
        row, state = await session_state(session, -1001, 0)
        state.update(context_started=now(), context_reset=0, sent_context=[
            {"id": 900, "speaker_id": 42, "text": "我跟着做两周，直接回血80%仓位", "bot": False},
            {"id": 901, "speaker_id": 999, "text": "八成是拉人进坑的", "bot": True, "reply_to": 900},
            {"id": 902, "speaker_id": 999, "text": "旧点评的后续", "bot": True, "reply_to": 901},
        ])
        store_state(session, row, -1001, 0, state)
    await chat.observe(message(1, "柴郡喵，周末看什么电影？", reply_to_message={"message_id": 900}))
    assert await chat.run_one()
    entries = model.calls[0][1]["messages"]
    assert [row["id"] for row in entries] == [1] and "reply_to" not in entries[0]


async def test_allowed_source_removed_during_generation_discards_response_without_archive(env):
    chat, model = await configured(env, archive_enabled=False, telegram_allowed_links=["https://t.me/Bitget_crypto1"])
    model.release = asyncio.Event()
    await chat.observe(message(1, "柴郡喵，今天吃什么？", **origin()))
    task = asyncio.create_task(chat.run_one())
    try:
        await asyncio.wait_for(model.started.wait(), 5)
        async with chat.db.transaction() as session:
            group = await session.get(Group, -1001)
            group.settings = {**group.settings, "telegram_allowed_links": []}
    finally:
        model.release.set()
        await task
    assert not any(method == "send_message" for method, _ in chat.tg.calls)
    async with chat.db.sessions() as session:
        assert (await session.scalar(select(ChatTurn))).state == "skipped"


async def test_no_archive_frozen_metadata_still_filters_after_rolling_row_is_gone(env):
    chat, model = await configured(env, archive_enabled=False, telegram_allowed_links=["https://t.me/Bitget_crypto1"])
    await chat.observe(message(1, "柴郡喵，今天吃什么？", **origin()))
    assert await chat.run_one()
    await age_reply(chat)
    async with chat.db.transaction() as session:
        row = await session.scalar(select(ChatMessage).where(ChatMessage.message_id == 1))
        # Reproduce rolling-window expiry without deleting any files or records.
        row.topic_id = 99
        group = await session.get(Group, -1001)
        group.settings = {**group.settings, "telegram_allowed_links": []}
    await chat.observe(message(2, "柴郡喵，周末去哪里玩？"))
    assert await chat.run_one()
    assert [row["id"] for row in model.calls[-1][1]["messages"]] == [2]
    assert "file_id" not in json.dumps(model.calls, ensure_ascii=False)


@pytest.mark.parametrize("archived", [False, True])
async def test_normal_reply_to_ad_can_ask_a_question_without_forwarding_ad_reference(env, archived):
    chat, model = await configured(env)
    source = message(700, "这句普通说明", photo=[{"file_id": "image"}], **origin())
    if archived:
        async with chat.db.transaction() as session:
            await capture(session, source)
    await chat.observe(message(1, "柴郡喵，这条消息怎么举报？", reply_to_message=source))
    assert await chat.run_one()
    target = model.calls[0][1]["messages"][-1]
    assert "quote" not in target and "reply_to" not in target


def test_origin_detection_does_not_confuse_normal_group_reply_with_external_reference():
    assert not channel_sources(channel_post())
    assert not channel_sources(message(1, "普通回复", reply_to_message=message(2, "普通原文", **origin())))
    assert not channel_sources(message(1, "用户转发", forward_origin={"type": "hidden_user", "sender_user_name": "A"}))
    assert "channel_sources" in json.loads(reply_snapshot(message(1, "普通原文", **origin()), direct=False))
    assert external_channel_reasons(channel_sources(message(1, "原文", **origin())), -1001,
                                    {"telegram_allowed_links": ["https://t.me/Bitget_crypto1/1"]})
