import asyncio

import pytest
from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import GetChat
from sqlalchemy import select

from conftest import drain, join, message
from purrai.emoji_status import pack_name
from purrai.management import Management
from purrai.models import Action, Group, Member, Rule, Verification, Whitelist, now
from purrai.runtime import Runtime
from purrai.service import Service, enqueue
from test_ad_filters import named
from test_verification import fixed_challenge, latest


@pytest.fixture(autouse=True)
async def approved_moderation_group(env):
    # These scenarios exercise moderation in a group already approved by its owner.
    async with env[1].transaction() as session:
        (await session.get(Group, -1001)).approved = True


async def name_rule(db):
    async with db.transaction() as session:
        session.add(Rule(group_id=-1001, kind="keyword", value="推广", scope="name"))


async def profile_api(telegram, monkeypatch, *, emoji="987", pack="bygbb666", expire=0):
    data = {
        "id": 42,
        "type": "private",
        "emoji_status_custom_emoji_id": emoji,
        "emoji_status_expiration_date": expire,
    }
    original = telegram.call

    async def call(method, **kwargs):
        if method == "get_chat" and kwargs["chat_id"] == 42:
            telegram.calls.append((method, kwargs))
            return dict(data)
        if method == "get_custom_emoji_stickers":
            telegram.calls.append((method, kwargs))
            return [{"custom_emoji_id": kwargs["custom_emoji_ids"][0], "set_name": pack}]
        if method == "get_sticker_set":
            return {"name": kwargs["name"], "sticker_type": "custom_emoji"}
        return await original(method, **kwargs)

    monkeypatch.setattr(telegram, "call", call)
    return data


async def test_renamed_member_latest_name_and_username_are_checked_every_message(env):
    _, db, tg, svc = env
    await name_rule(db)
    await svc.message(named(1, "大家好", "正常名字"))
    await svc.message(named(2, "正常正文", "推广名字"))
    await drain(svc)
    assert tg.members[(-1001, 42)]["until_date"] == 0
    assert [k["message_id"] for m, k in tg.calls if m == "delete_message"] == [2]
    async with db.transaction() as session:
        session.add(Rule(group_id=-1001, kind="block_mention", value="badpromo"))
    incoming = message(3, "正文没有广告", user_id=43)
    incoming["from"].update(first_name="正常", username="badpromo")
    await svc.message(incoming)
    await drain(svc)
    assert tg.members[(-1001, 43)]["until_date"] == 0
    assert any(m == "delete_message" and k["message_id"] == 3 for m, k in tg.calls)


async def test_rename_during_verification_cannot_be_released_by_old_buttons(env, monkeypatch):
    verification = await fixed_challenge(env, monkeypatch)
    _, db, tg, svc = env
    await name_rule(db)
    await svc.message(named(50, "你好", "推广名字"))
    await drain(svc)
    row = await latest(db)
    assert row.state == "failed" and row.failure_action == "permanent_mute"
    assert tg.members[(-1001, 42)]["until_date"] == 0
    await svc.callback(
        {"id": "old", "from": {"id": 42}, "data": f"v:{verification.id}:{verification.nonce}:1"}
    )
    assert (await latest(db)).input_digits == ""


async def test_admin_unmute_success_trusts_all_rules_and_survives_restart_rejoin(env, monkeypatch):
    settings, db, tg, svc = env
    await name_rule(db)
    await svc.message(named(1, "正文", "推广名字"))
    await drain(svc)
    await Management(db, tg, svc, settings).member_action(1, -1001, 42, "unmute", "trust-this-user", "误判")
    await drain(svc)
    async with db.sessions() as session:
        white = await session.get(Whitelist, (-1001, 42))
        assert white.verification and white.moderation
    restarted = Service(db, tg, settings)
    await restarted.member_event(join(old="member", new="left"))
    incoming = join(date=now() + 1)
    incoming["new_chat_member"]["user"].update(first_name="推广", is_premium=True)
    await restarted.member_event(incoming)
    tg.calls.clear()
    incoming = named(10, "https://spam.example @unknown", "推广")
    incoming["from"]["is_premium"] = True
    incoming["photo"] = [{"file_id": "x"}]
    await restarted.message(incoming)
    await drain(restarted)
    assert not tg.calls


async def test_failed_manual_unmute_does_not_create_whitelist(env):
    settings, db, tg, svc = env
    await name_rule(db)
    await svc.message(named(1, "正文", "推广"))
    await drain(svc)
    tg.fail["restrict_chat_member"] = 1
    await Management(db, tg, svc, settings).member_action(1, -1001, 42, "unmute", "failed-unmute", "误判")
    await svc.run_one_action()
    async with db.sessions() as session:
        assert await session.get(Whitelist, (-1001, 42)) is None


def unmute_event(tg, *, actor=1, actor_bot=False):
    return {
        "chat": {"id": -1001, "type": "supergroup"},
        "date": int(now()),
        "from": {"id": actor, "is_bot": actor_bot},
        "old_chat_member": {
            "status": "restricted",
            "is_member": True,
            "can_send_messages": False,
            "until_date": 0,
            "user": {"id": 42},
        },
        "new_chat_member": {"status": "member", "user": {"id": 42}},
    }


async def test_telegram_human_admin_unmute_trusts_and_cancels_queued_penalties(env):
    _, db, tg, svc = env
    await name_rule(db)
    await svc.message(named(1, "待删正文", "推广"))
    tg.members[(-1001, 7)] = {"status": "administrator"}
    await svc.member_event(unmute_event(tg, actor=7), event_id=50)
    tg.calls.clear()
    await svc.message(named(2, "广告消息", "推广"))
    await drain(svc)
    assert not tg.calls
    async with db.sessions() as session:
        assert (await session.get(Whitelist, (-1001, 42))).moderation
        assert all(
            row.state == "cancelled"
            for row in await session.scalars(
                select(Action).where(Action.kind.in_(("delete", "profile_mute")))
            )
        )


@pytest.mark.parametrize(
    "actor,bot,status", [(42, False, "member"), (999, True, "administrator"), (777, True, "administrator")]
)
async def test_auto_expiry_self_change_or_other_bot_never_grants_trust(env, actor, bot, status):
    _, db, tg, svc = env
    tg.members[(-1001, actor)] = {"status": status}
    await svc.member_event(unmute_event(tg, actor=actor, actor_bot=bot), event_id=60)
    async with db.sessions() as session:
        assert await session.get(Whitelist, (-1001, 42)) is None


async def test_manual_release_cancels_pending_timeout_and_skips_every_automatic_rule(env, monkeypatch):
    await fixed_challenge(env, monkeypatch)
    settings, db, tg, svc = env
    await Management(db, tg, svc, settings).member_action(
        1, -1001, 42, "release", "manual-trust-release", "认识的群友"
    )
    await drain(svc)
    await name_rule(db)
    tg.calls.clear()
    await svc.message(named(5, "广告", "推广"))
    await svc.scan_timeouts()
    await drain(svc)
    assert not tg.calls and (await latest(db)).state == "passed"
    async with db.sessions() as session:
        assert (await session.get(Whitelist, (-1001, 42))).verification


async def test_automatic_captcha_pass_never_whitelists(env, monkeypatch):
    from test_verification import enter_code

    verification = await fixed_challenge(env, monkeypatch)
    _, db, _, svc = env
    await enter_code(svc, verification, "123456")
    await drain(svc)
    async with db.sessions() as session:
        assert await session.get(Whitelist, (-1001, 42)) is None


async def test_moderation_only_whitelist_preserves_pending_verification(env, monkeypatch):
    _, db, tg, svc = env
    async with db.transaction() as session:
        session.add(Whitelist(group_id=-1001, user_id=42, verification=False, moderation=True))
    await fixed_challenge(env, monkeypatch)
    await name_rule(db)
    await svc.message(named(6, "正文", "推广"))
    await drain(svc)
    assert (await latest(db)).state == "pending"
    assert not tg.members[(-1001, 42)]["can_send_messages"]
    assert [args["message_id"] for method, args in tg.calls if method == "delete_message"] == [6]
    async with db.sessions() as session:
        assert not (await session.get(Whitelist, (-1001, 42))).verification


@pytest.mark.parametrize("pack", ["bygbb666", "GsHeChaba"])
async def test_emoji_pack_match_permanently_mutes_and_ends_captcha(env, monkeypatch, pack):
    _, db, tg, svc = env
    await profile_api(tg, monkeypatch, pack=pack)
    incoming = join()
    incoming["new_chat_member"]["user"]["is_premium"] = True
    await svc.member_event(incoming)
    await drain(svc)
    assert tg.members[(-1001, 42)]["until_date"] == 0
    async with db.sessions() as session:
        member = await session.get(Member, (-1001, 42))
        assert member.owned_restriction["kind"] == "emoji_status"
        assert (await session.scalar(select(Verification))).state == "failed"
        mute = await session.scalar(select(Action).where(Action.kind == "emoji_status_mute"))
        assert mute.state == "succeeded"
    assert not any(m == "ban_chat_member" for m, _ in tg.calls)


@pytest.mark.parametrize(
    "emoji,pack,expires", [("", "bygbb666", 0), ("987", "safe_pack", 0), ("987", "bygbb666", 1)]
)
async def test_safe_missing_or_expired_status_does_not_mute(env, monkeypatch, emoji, pack, expires):
    _, db, tg, svc = env
    await profile_api(tg, monkeypatch, emoji=emoji, pack=pack, expire=expires)
    incoming = message(1, "大家好")
    incoming["from"]["is_premium"] = True
    await svc.message(incoming)
    await drain(svc)
    assert not any(m == "restrict_chat_member" for m, _ in tg.calls)


async def test_changed_status_on_later_message_is_rechecked(env, monkeypatch):
    _, db, tg, svc = env
    profile = await profile_api(tg, monkeypatch, emoji="")
    for identifier in (1, 2):
        incoming = message(identifier, f"正常文字{identifier}")
        incoming["from"]["is_premium"] = True
        await svc.message(incoming)
        await drain(svc)
        profile["emoji_status_custom_emoji_id"] = "987"
    assert tg.members[(-1001, 42)]["until_date"] == 0


async def test_status_changes_between_check_and_mute_cancel_punishment(env, monkeypatch):
    _, db, tg, svc = env
    profile = await profile_api(tg, monkeypatch)
    incoming = message(1, "大家好")
    incoming["from"]["is_premium"] = True
    await svc.message(incoming)
    await svc.emoji_status.run_one()
    profile["emoji_status_custom_emoji_id"] = ""
    await drain(svc)
    assert not any(m == "restrict_chat_member" for m, _ in tg.calls)


async def test_status_profile_unavailable_is_visible_failure_without_mute(env, monkeypatch):
    _, db, tg, svc = env
    incoming = message(1, "正常")
    incoming["from"]["is_premium"] = True
    await svc.message(incoming)
    await drain(svc)
    async with db.sessions() as session:
        row = await session.scalar(select(Action).where(Action.kind == "emoji_status_check"))
        assert row.state == "failed" and "未执行禁言" in row.error
    assert not any(m == "restrict_chat_member" for m, _ in tg.calls)


async def test_slow_status_lookup_does_not_hold_writer_and_respects_new_whitelist(env, monkeypatch):
    _, db, tg, svc = env
    await profile_api(tg, monkeypatch)
    incoming = message(1, "普通文字")
    incoming["from"]["is_premium"] = True
    await svc.message(incoming)
    entered, finish = asyncio.Event(), asyncio.Event()
    original = tg.call

    async def slow(method, **kwargs):
        if method == "get_chat" and kwargs["chat_id"] == 42:
            entered.set()
            await finish.wait()
        return await original(method, **kwargs)

    monkeypatch.setattr(tg, "call", slow)
    task = asyncio.create_task(svc.emoji_status.run_one())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        async with asyncio.timeout(1):
            async with db.transaction() as session:
                session.add(Whitelist(group_id=-1001, user_id=42, moderation=True))
    finally:
        finish.set()
        await task
    await drain(svc)
    assert not any(m == "restrict_chat_member" for m, _ in tg.calls)


async def test_status_rate_limit_persists_retry_after(env, monkeypatch):
    _, db, tg, svc = env
    original = tg.call

    async def limited(method, **kwargs):
        if method == "get_chat" and kwargs["chat_id"] == 42:
            raise TelegramRetryAfter(method=GetChat(chat_id=42), message="limited", retry_after=13)
        return await original(method, **kwargs)

    monkeypatch.setattr(tg, "call", limited)
    incoming = message(1, "正常")
    incoming["from"]["is_premium"] = True
    await svc.message(incoming)
    await svc.emoji_status.run_one()
    async with db.sessions() as session:
        row = await session.scalar(select(Action).where(Action.kind == "emoji_status_check"))
        assert row.state == "pending" and row.available_at >= now() + 12


async def test_pack_configuration_normalizes_validates_and_can_clear(env, monkeypatch, client):
    _, _, tg, _ = env
    await profile_api(tg, monkeypatch)
    response = await client.patch(
        "/api/groups/-1001/settings",
        json={"settings": {"emoji_status_blocked_packs": ["https://t.me/addemoji/ByGbb666"]}},
    )
    assert response.status_code == 200
    assert response.json()["settings"]["emoji_status_blocked_packs"] == ["bygbb666"]
    response = await client.patch(
        "/api/groups/-1001/settings", json={"settings": {"emoji_status_blocked_packs": []}}
    )
    assert response.status_code == 200 and response.json()["settings"]["emoji_status_blocked_packs"] == []
    response = await client.patch(
        "/api/groups/-1001/settings", json={"settings": {"emoji_status_blocked_packs": ["@TGDCVIP"]}}
    )
    assert response.status_code == 422


async def test_bot_can_manage_status_pack_blacklist(env, monkeypatch):
    from test_bot_admin import click, deliver, open_group, private

    _, db, tg, svc = env
    await profile_api(tg, monkeypatch)
    runtime = Runtime(db, tg, svc)
    await open_group(runtime, tg)
    await click(runtime, tg, "防广告设置")
    await click(runtime, tg, "状态表情包黑名单")
    await click(runtime, tg, "新增表情包")
    await deliver(runtime, {"message": private("https://t.me/addemoji/AnotherPack")})
    async with db.sessions() as session:
        assert (await session.get(Group, -1001)).settings["emoji_status_blocked_packs"] == [
            "bygbb666",
            "gshechaba",
            "anotherpack",
        ]
    await click(runtime, tg, "新增表情包")
    await deliver(runtime, {"message": private("-")})
    async with db.sessions() as session:
        assert len((await session.get(Group, -1001)).settings["emoji_status_blocked_packs"]) == 3
    await deliver(runtime, {"message": private("https://t.me/addemoji/ANOTHERPACK")})
    await click(runtime, tg, "移除 anotherpack")
    async with db.sessions() as session:
        assert (await session.get(Group, -1001)).settings["emoji_status_blocked_packs"] == [
            "bygbb666",
            "gshechaba",
        ]
    await click(runtime, tg, "清空状态表情包黑名单")
    await click(runtime, tg, "确认清空")
    async with db.sessions() as session:
        assert (await session.get(Group, -1001)).settings["emoji_status_blocked_packs"] == []


def test_pack_identifiers_only_use_canonical_telegram_links():
    assert pack_name("https://t.me/addemoji/ByGbb666") == "bygbb666"
    for value in ("https://example.com/addemoji/bygbb666", "https://t.me/addstickers/bygbb666", "@TGDCVIP"):
        with pytest.raises(ValueError):
            pack_name(value)


async def test_lost_permanent_mute_response_recovers_without_duplicate_restriction(env, monkeypatch):
    await fixed_challenge(env, monkeypatch)
    _, db, tg, svc = env
    await name_rule(db)
    await svc.message(named(10, "普通消息", "推广"))
    original = tg.call
    lost = False

    async def uncertain(method, **kwargs):
        nonlocal lost
        result = await original(method, **kwargs)
        if method == "restrict_chat_member" and not lost:
            lost = True
            raise ConnectionError("response lost")
        return result

    monkeypatch.setattr(tg, "call", uncertain)
    tg.calls.clear()
    await svc.run_one_action()
    await svc.run_one_action()
    assert (await latest(db)).state == "failing"
    async with db.transaction() as session:
        action = await session.scalar(select(Action).where(Action.kind == "profile_mute"))
        assert action.state == "pending"
        action.available_at = 0
    await drain(svc)
    assert (await latest(db)).state == "failed"
    assert len([m for m, _ in tg.calls if m == "restrict_chat_member"]) == 1


async def test_manual_unmute_response_loss_only_trusts_after_confirmed_retry(env, monkeypatch):
    settings, db, tg, svc = env
    await name_rule(db)
    await svc.message(named(1, "消息", "推广"))
    await drain(svc)
    await Management(db, tg, svc, settings).member_action(
        1, -1001, 42, "unmute", "lost-unmute-response", "人工放行"
    )
    original = tg.call
    lost = False

    async def uncertain(method, **kwargs):
        nonlocal lost
        result = await original(method, **kwargs)
        if method == "restrict_chat_member" and not lost:
            lost = True
            raise ConnectionError("response lost")
        return result

    monkeypatch.setattr(tg, "call", uncertain)
    await svc.run_one_action()
    async with db.transaction() as session:
        assert await session.get(Whitelist, (-1001, 42)) is None
        action = await session.scalar(select(Action).where(Action.kind == "unmute"))
        action.available_at = 0
    await drain(svc)
    async with db.sessions() as session:
        assert (await session.get(Whitelist, (-1001, 42))).moderation


async def test_emoji_mute_response_loss_finalizes_even_after_user_removes_status(env, monkeypatch):
    await fixed_challenge(env, monkeypatch)
    _, db, tg, svc = env
    status = await profile_api(tg, monkeypatch)
    original, lost = tg.call, False

    async def uncertain(method, **kwargs):
        nonlocal lost
        result = await original(method, **kwargs)
        if method == "restrict_chat_member" and not lost:
            lost = True
            raise ConnectionError("response lost")
        return result

    monkeypatch.setattr(tg, "call", uncertain)
    incoming = message(10, "正文")
    incoming["from"]["is_premium"] = True
    await svc.message(incoming)
    await drain(svc)
    assert (await latest(db)).state == "failing"
    assert tg.members[(-1001, 42)]["until_date"] == 0
    status["emoji_status_custom_emoji_id"] = ""
    async with db.transaction() as session:
        action = await session.scalar(select(Action).where(Action.kind == "emoji_status_mute"))
        assert action.state == "pending"
        action.available_at = 0
    tg.calls.clear()
    await drain(svc)
    assert (await latest(db)).state == "failed"
    assert not any(method == "restrict_chat_member" for method, _ in tg.calls)
    async with db.sessions() as session:
        assert (await session.get(Action, action.id)).state == "succeeded"


@pytest.mark.parametrize("source", ["text", "photo", "private_cleanup"])
async def test_emoji_message_deletion_and_whitelist_release(env, monkeypatch, source):
    settings, db, tg, svc = env
    await profile_api(tg, monkeypatch)
    incoming = message(1, "正文")
    incoming["from"]["is_premium"] = True
    if source == "photo":
        incoming["photo"] = [{"file_id": "image"}]
    elif source == "private_cleanup":
        async with db.transaction() as session:
            await enqueue(
                session,
                "delete",
                "cleanup:private-prompt",
                -1001,
                42,
                {"chat_id": 42, "message_id": 1},
                priority=0,
            )
    await svc.message(incoming)
    await drain(svc)
    assert tg.members[(-1001, 42)]["until_date"] == 0
    deletions = [args for method, args in tg.calls if method == "delete_message" and args["chat_id"] == -1001]
    assert len(deletions) == 1 and deletions[0]["message_id"] == 1
    await Management(db, tg, svc, settings).member_action(
        1, -1001, 42, "unmute", "release-emoji-status", "误判"
    )
    await drain(svc)
    tg.calls.clear()
    incoming["message_id"] = 2
    await svc.message(incoming)
    await drain(svc)
    assert not tg.calls


async def test_trust_is_group_local_and_other_admin_restriction_is_preserved(env):
    settings, db, tg, svc = env
    await name_rule(db)
    await svc.message(named(1, "正文", "推广"))
    await drain(svc)
    tg.members[(-1001, 42)]["until_date"] = int(now() + 3600)
    await Management(db, tg, svc, settings).member_action(1, -1001, 42, "unmute", "conflict-unmute", "误判")
    await drain(svc)
    async with db.sessions() as session:
        assert await session.get(Whitelist, (-1001, 42)) is None
    await svc.member_event(unmute_event(tg), event_id=600)
    async with db.transaction() as session:
        session.add(Group(approved=True, id=-1002, title="其他群", enabled=True))
        session.add(Rule(group_id=-1002, kind="keyword", value="推广", scope="name"))
    incoming = named(2, "普通消息", "推广")
    incoming["chat"]["id"] = -1002
    await svc.message(incoming)
    await drain(svc)
    assert tg.members[(-1002, 42)]["until_date"] == 0
