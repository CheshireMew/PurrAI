"""Alternate Telegram sender identities and message surfaces reach the same review."""
import copy

import pytest

from conftest import drain, message
from test_advertisement_review import records, reviewer, add_sample
from purrai.models import Action, AdvertisementReview, AdvertisementSample, ArchivedMessage, Group, Member, Violation
from purrai.text_matching import message_content


def channel_message(identifier=1, **values):
    return message(identifier, values.pop("text", "今天有一条待审核内容"), **{
        "from": {"id": 136817688, "is_bot": True, "first_name": "Channel"},
        "sender_chat": {"id": -2002, "type": "channel", "title": "测试群"}, **values})


async def settings(db, **changes):
    async with db.transaction() as session:
        group = await session.get(Group, -1001)
        group.settings = {**group.settings, **changes}


def link_channel(tg, channel_id):
    original = tg.call
    async def call(method, **kwargs):
        result = await original(method, **kwargs)
        if method == "get_chat" and kwargs["chat_id"] == -1001:
            result["linked_chat_id"] = channel_id
        return result
    tg.call = call


async def test_direct_spoofed_channel_is_reviewed_deleted_and_banned(env):
    _, db, tg, service = env
    worker, model = await reviewer(env)
    msg = channel_message(caption="一条图片说明 https://example.org/Path", text="",
        photo=[{"file_id": "offline"}], caption_entities=[{"type": "pre", "offset": 7, "length": 24}])
    await service.message(msg)
    await worker.run_one()
    await drain(service)
    assert model.calls[0][0]["name"] == "测试群"
    assert model.calls[0][0]["links"] == ["https://example.org/Path"]
    assert [(m, k.get("sender_chat_id")) for m, k in tg.calls if m == "ban_chat_sender_chat"] == [("ban_chat_sender_chat", -2002)]
    assert [k["message_id"] for m, k in tg.calls if m == "delete_message"] == [1]
    assert not any(m in ("restrict_chat_member", "ban_chat_member", "get_chat_member") and k.get("user_id") in (-2002, 136817688) for m, k in tg.calls)
    assert not await records(db, Member)
    violation, = await records(db, Violation)
    assert violation.sender_chat and violation.user_id == -2002


@pytest.mark.parametrize("trusted_kind", ["anonymous", "linked", "linked_direct"])
async def test_trusted_channel_ids_are_exempt(env, trusted_kind):
    _, db, tg, service = env
    worker, model = await reviewer(env)
    await settings(db, inline_buttons_ban=True)
    msg = channel_message(reply_markup={"inline_keyboard": [[{"text": "打开", "url": "https://example.org"}]]})
    if trusted_kind == "anonymous":
        msg["sender_chat"]["id"] = -1001
    else:
        link_channel(tg, -2002)
        msg["is_automatic_forward"] = trusted_kind == "linked"
    await service.message(msg)
    await worker.run_one()
    await drain(service)
    assert not model.calls
    assert not await records(db, Violation)
    assert not any(m in ("delete_message", "ban_chat_sender_chat") for m, _ in tg.calls)


async def test_automatic_forward_flag_alone_is_not_a_trusted_id(env):
    _, _, tg, service = env
    worker, _ = await reviewer(env)
    link_channel(tg, -3003)
    await service.message(channel_message(is_automatic_forward=True))
    await worker.run_one()
    await drain(service)
    assert any(m == "ban_chat_sender_chat" for m, _ in tg.calls)


@pytest.mark.parametrize("caller", ["human", "channel", "unknown"])
async def test_guest_caller_identity_is_used_for_review(env, caller):
    _, db, tg, service = env
    worker, model = await reviewer(env)
    msg = message(1, "今天有另一条待审核内容", **{"from": {"id": 777, "is_bot": True, "first_name": "代发工具"}})
    if caller == "human":
        msg["guest_bot_caller_user"] = {"id": 42, "is_bot": False, "first_name": "发起人"}
    elif caller == "channel":
        msg["guest_bot_caller_chat"] = {"id": -2002, "type": "channel", "title": "发起频道"}
    await service.message(msg)
    await worker.run_one()
    await drain(service)
    row, = await records(db, AdvertisementReview)
    archive, = await records(db, ArchivedMessage)
    assert row.user_id == archive.sender_id == {"human": 42, "channel": -2002, "unknown": 777}[caller]
    assert model.calls[0][0]["name"] == {"human": "发起人", "channel": "发起频道", "unknown": "代发工具"}[caller]
    assert any(m == "delete_message" for m, _ in tg.calls)
    assert not any(m == "get_chat_member" and k["user_id"] < 0 for m, k in tg.calls)


@pytest.mark.parametrize("caller", ["human", "channel"])
async def test_guest_buttons_punish_the_caller_and_not_the_proxy(env, caller):
    _, db, tg, service = env
    await settings(db, inline_buttons_ban=True)
    msg = message(1, "1", **{"from": {"id": 777, "is_bot": True},
        "reply_markup": {"inline_keyboard": [[{"text": "打开", "url": "https://example.org"}]]}})
    field, identity = ("guest_bot_caller_user", {"id": 42, "is_bot": False}) if caller == "human" else ("guest_bot_caller_chat", {"id": -2002, "type": "channel"})
    msg[field] = identity
    await service.message(msg)
    await drain(service)
    mutations = [(m, k) for m, k in tg.calls if m in ("ban_chat_member", "ban_chat_sender_chat")]
    assert len(mutations) == 1
    assert mutations[0][1].get("user_id", mutations[0][1].get("sender_chat_id")) == identity["id"]


async def test_clean_external_channel_is_not_banned(env):
    _, _, tg, service = env
    worker, _ = await reviewer(env, lambda row: False)
    await service.message(channel_message())
    await worker.run_one()
    await drain(service)
    assert not any(m in ("delete_message", "ban_chat_sender_chat") for m, _ in tg.calls)


@pytest.mark.parametrize("change", ["delete_only", "linked", "disabled", "edited"])
async def test_channel_punishment_rechecks_current_settings_and_message(env, change):
    _, db, tg, service = env
    worker, _ = await reviewer(env)
    msg = channel_message()
    await service.message(msg)
    await worker.run_one()
    if change == "delete_only":
        await settings(db, channel_ad_penalty="delete")
    elif change == "linked":
        link_channel(tg, -2002)
    elif change == "disabled":
        async with db.transaction() as session:
            (await session.get(Group, -1001)).enabled = False
    else:
        await service.message({**msg, "text": "修改后的普通文字", "edit_date": msg["date"] + 1}, edited=True, update_id=2)
    await drain(service)
    assert not any(m == "ban_chat_sender_chat" for m, _ in tg.calls)
    assert any(m == "delete_message" for m, _ in tg.calls) == (change == "delete_only")


async def test_channel_local_sample_uses_channel_api(env):
    _, _, tg, service = env
    text = "这是一段可以替换的长文本供样本独立验证使用"
    await add_sample(env, text)
    await service.message(channel_message(text=text))
    await drain(service)
    assert any(m == "ban_chat_sender_chat" for m, _ in tg.calls)


async def test_hidden_links_and_button_edits_reach_ai_without_poisoning_text_samples(env):
    _, db, _, service = env
    worker, model = await reviewer(env)
    msg = message(1, "点这里", entities=[{"type": "text_link", "offset": 0, "length": 3, "url": "https://example.org/A"}])
    await service.message(msg)
    await worker.run_one()
    assert model.calls[0][0]["links"] == ["https://example.org/A"]
    sample, = await records(db, AdvertisementSample)
    assert sample.scope == "context"
    edit = copy.deepcopy(msg)
    edit.update(edit_date=msg["date"] + 1, reply_markup={"inline_keyboard": [[{"text": "详情", "url": "https://example.net/B"}]]})
    await service.message(edit, edited=True, update_id=2)
    await worker.run_one()
    assert model.calls[1][0]["buttons"] == [{"text": "详情", "url": "https://example.net/B"}]
    assert {r.state for r in await records(db, AdvertisementReview)} == {"superseded", "advertisement"}
    old = [a for a in await records(db, Action) if a.kind == "delete"][0]
    await drain(service)
    assert next(a for a in await records(db, Action) if a.id == old.id).state == "cancelled"


def test_code_hidden_unicode_and_button_urls_preserve_case_and_ignore_quoted_buttons():
    msg = message(1, "😀 ｈｔｔｐｓ：／／Example.org/AbC\u200b", entities=[{"type": "pre", "offset": 3, "length": 24}])
    assert message_content(msg)[1] == ["https://Example.org/AbC"]
    msg = message(2, "😀 点击", entities=[{"type": "url", "offset": 3, "length": 2}])
    assert message_content(msg)[1] == ["点击"]  # Telegram offsets count UTF-16 code units.
    msg = message(3, "普通回复", reply_to_message=message(2, "原消息", reply_markup={"inline_keyboard": [[{"text": "打开", "url": "https://example.org"}]]}))
    assert message_content(msg)[1] == []


@pytest.mark.parametrize("correction", [False, True])
async def test_channel_ban_can_be_reversed_from_its_violation(env, client, correction):
    _, db, tg, service = env
    worker, _ = await reviewer(env)
    await service.message(channel_message())
    await worker.run_one()
    await drain(service)
    violation, = await records(db, Violation)
    from purrai.record_operations import violation_operations
    assert violation_operations({"sender_chat": True, "actions": [
        {"kind": "channel_ad_ban", "state": "succeeded"}]}, True) == ["unban"]
    if correction:
        from purrai.management import Management
        from purrai.schemas import ViolationCorrectionInput
        await Management(db, tg, settings=env[0]).corrections.apply(1, -1001, violation.id,
            ViolationCorrectionInput(action="unban", reason="人工复核误判", request_id="channel-correct"))
    else:
        from purrai.schemas import ActionInput
        from purrai.management import Management
        await Management(db, tg, settings=env[0]).violation_action(1, -1001, violation.id,
            ActionInput(action="unban", reason="人工解除频道封禁", request_id="channel-unban"))
    await drain(service)
    assert [k["sender_chat_id"] for m, k in tg.calls if m == "unban_chat_sender_chat"] == [-2002]
    assert not any(m == "unban_chat_member" for m, _ in tg.calls)
    assert violation_operations({"sender_chat": True, "actions": [
        {"kind": "channel_ad_ban", "state": "succeeded"}, {"kind": "channel_unban", "state": "succeeded"}]}, True) == []


@pytest.mark.parametrize("surface", ["code", "button", "hidden", "caption"])
async def test_channel_identity_cannot_skip_existing_external_group_link_policy(env, surface):
    _, db, tg, service = env
    original = tg.call
    async def target(method, **kwargs):
        if method == "get_chat" and kwargs.get("chat_id") == "@examplechat":
            return {"id": -7007, "type": "supergroup"}
        return await original(method, **kwargs)
    tg.call = target
    msg = channel_message()
    if surface == "hidden":
        msg["entities"] = [{"type": "text_link", "offset": 0, "length": 2, "url": "https://t.me/examplechat"}]
    elif surface == "button":
        msg["reply_markup"] = {"inline_keyboard": [[{"text": "详情", "url": "https://t.me/examplechat"}]]}
    elif surface == "caption":
        msg.update(text="", caption="https://t.me/examplechat", caption_entities=[{"type": "code", "offset": 0, "length": 24}])
    else:
        msg.update(text="https://t.me/examplechat", entities=[{"type": "pre", "offset": 0, "length": 24}])
    await service.message(msg)
    await drain(service)
    assert any(m == "ban_chat_sender_chat" for m, _ in tg.calls)
    assert any(m == "delete_message" for m, _ in tg.calls)
    assert not any(m == "get_chat_member" and k["user_id"] < 0 for m, k in tg.calls)
    assert not await records(db, Member)


async def test_channel_links_removed_by_edit_cancel_queued_link_ban(env):
    _, db, tg, service = env
    original = tg.call
    async def target(method, **kwargs):
        if method == "get_chat" and kwargs.get("chat_id") == "@examplechat":
            return {"id": -7007, "type": "supergroup"}
        return await original(method, **kwargs)
    tg.call = target
    msg = channel_message(text="https://t.me/examplechat")
    await service.message(msg)
    await service.advertising.run_one()
    assert any(a.kind == "channel_ad_ban" for a in await records(db, Action))
    await service.message({**msg, "text": "新的正常文字", "edit_date": msg["date"] + 1}, edited=True, update_id=2)
    await drain(service)
    assert not any(m in ("ban_chat_sender_chat", "delete_message") for m, _ in tg.calls)
