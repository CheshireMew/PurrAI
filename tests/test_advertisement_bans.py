import asyncio

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import GetChat
from sqlalchemy import select

from conftest import drain, join, message
from purrai.models import Action, Group, Inbox, Member, Rule, Violation, Whitelist, now
from purrai.service import Service
from purrai.telegram_links import parse_link

INVITE = "https://t.me/+9hXB_sUYRDE2NTFh"
OWN = "https://t.me/+OwnInvite123456"
TITLE = "📣 以太坊合约免费跟单实时策略"


async def setup(env, monkeypatch):
    _, db, tg, _ = env
    async with db.transaction() as session:
        session.add(Member(group_id=-1001, user_id=42, joined_at=now() - 30 * 86400,
                           passed_at=now() - 30 * 86400, observed_messages=10))
    original = tg.call

    async def call(method, **kwargs):
        result = await original(method, **kwargs)
        if method != "get_chat":
            return result
        target = kwargs["chat_id"]
        if target == -1001:
            return {**result, "id": -1001, "type": "supergroup", "username": "our_group", "invite_link": OWN}
        profiles = {"@other_group": {"id": -9001, "type": "supergroup"},
                    "@news_feed": {"id": -8001, "type": "channel"},
                    "@normal_user": {"id": 88, "type": "private"},
                    -1009001: {"id": -1009001, "type": "supergroup"}}
        if target in profiles:
            return profiles[target]
        raise TelegramBadRequest(method=GetChat(chat_id=target), message="chat not found")

    monkeypatch.setattr(tg, "call", call)
    return call


@pytest.mark.parametrize("body,extra", [
    ("来内部群学习老师的分析逻辑，让每一次开仓都有依据。", {}),
    ("每天精准分析BTC-ETH 策略", {"reply_to_message": {"message_id": 9, "forward_origin": {
        "type": "channel", "chat": {"id": -7001, "type": "channel", "title": TITLE}}}}),
    (INVITE + "\nBTC大波动要来了，你准备好了吗", {}),
    ("不是广告，来内部群学习老师的分析逻辑", {}),
    ("免费带单，私聊我领取策略", {}),
])
async def test_screenshot_solicitations_delete_and_permanently_ban_older_members(env, monkeypatch, body, extra):
    await setup(env, monkeypatch)
    _, db, tg, svc = env
    await svc.message(message(10, body, **extra))
    await drain(svc)
    assert tg.members[(-1001, 42)] == {"status": "kicked", "until_date": 0}
    assert len([1 for name, _ in tg.calls if name == "ban_chat_member"]) == 1
    assert not any(name == "unban_chat_member" for name, _ in tg.calls)
    assert [args["message_id"] for name, args in tg.calls if name == "delete_message"] == [10]
    async with db.sessions() as session:
        ban = await session.scalar(select(Action).where(Action.kind == "advertisement_ban"))
        assert ban.state == "succeeded"
        row = await session.get(Violation, ban.payload["violation_id"])
        assert row.message_violation and not row.warning_active


@pytest.mark.parametrize("body", [
    "BTC大波动要来了，你准备好了吗", "每天分析 BTC 和 ETH 的交易策略",
    "老师的分析逻辑值得学习，我这次 BTC 开仓有依据了", "我想加群学习交易，该注意些什么？",
    "如何识别私聊返佣广告？", "来本群学习老师的分析逻辑",
    "这个免费跟单频道是不是骗子？", "“来内部群学习老师的分析逻辑”，这种广告请小心",
    "我昨天加入了投资群，感觉都是骗局", "https://t.me/news_feed/123 每日市场新闻",
    "https://t.me/normal_user 这位是我的朋友", "https://t.me/addemoji/bygbb666",
    "https://t.me/c/1/123 本群的消息", "https://t.me/our_group", OWN,
    "他们说免费带单让我私聊我该怎么辨别是不是骗子？",
    "这种策略不可能稳赚，进群之前最好查清楚",
    "我不做返佣，别私聊我", "“免费带单，私聊我” 这种广告太多了",
])
async def test_normal_discussions_news_and_own_links_are_not_banned(env, monkeypatch, body):
    await setup(env, monkeypatch)
    _, _, tg, svc = env
    await svc.message(message(10, body))
    await drain(svc)
    assert not any(name in ("delete_message", "ban_chat_member", "restrict_chat_member") for name, _ in tg.calls)


@pytest.mark.parametrize("url", [INVITE, "https://telegram.me/joinchat/9hXB_sUYRDE2NTFh",
    "tg://join?invite=9hXB_sUYRDE2NTFh", "https://t.me/other_group", "tg://resolve?domain=other_group",
    "https://other_group.t.me/123", "https://t.me/c/9001/123",
    "https://example.com/?redirect=https%3A%2F%2Ft.me%2F%2B9hXB_sUYRDE2NTFh"])
async def test_external_group_and_private_invites_are_banned_in_all_supported_forms(env, monkeypatch, url):
    await setup(env, monkeypatch)
    _, _, tg, svc = env
    await svc.message(message(10, url))
    await drain(svc)
    assert tg.members[(-1001, 42)]["status"] == "kicked"


async def test_hidden_link_added_in_caption_edit_is_checked_and_only_bans_once(env, monkeypatch):
    await setup(env, monkeypatch)
    _, _, tg, svc = env
    await svc.message(message(10, "原文"), update_id=1)
    incoming = message(10, "", caption="😀 点这里", photo=[{"file_id": "photo"}],
        caption_entities=[{"type": "text_link", "offset": 3, "length": 3, "url": INVITE}])
    await svc.message(incoming, edited=True, update_id=2)
    await svc.message(incoming, edited=True, update_id=2)
    await drain(svc)
    assert len([1 for name, _ in tg.calls if name == "ban_chat_member"]) == 1


@pytest.mark.parametrize("body", ["举报：这个人发广告 " + INVITE,
    INVITE + " 这个群是骗子，大家别进", "“来内部群学习老师的分析逻辑” 这种广告请警惕 " + INVITE])
async def test_explicit_scam_reports_remove_the_live_invite_without_permanent_ban(env, monkeypatch, body):
    await setup(env, monkeypatch)
    _, _, tg, svc = env
    await svc.message(message(10, body))
    await drain(svc)
    assert not any(name == "ban_chat_member" for name, _ in tg.calls)
    assert any(name == "delete_message" for name, _ in tg.calls)


@pytest.mark.parametrize("exemption", ["administrator", "whitelist", "superadmin", "allowed_link", "allowed_mention", "previous_own_link"])
async def test_telegram_link_exemptions(env, monkeypatch, exemption):
    await setup(env, monkeypatch)
    settings, db, tg, svc = env
    url = "https://t.me/other_group" if exemption == "allowed_mention" else INVITE
    async with db.transaction() as session:
        if exemption == "whitelist":
            session.add(Whitelist(group_id=-1001, user_id=42, moderation=True))
        elif exemption == "allowed_link":
            group = await session.get(Group, -1001)
            group.settings = {**group.settings, "telegram_allowed_links": [INVITE]}
        elif exemption == "allowed_mention":
            session.add(Rule(group_id=-1001, kind="allow_mention", value="other_group"))
        elif exemption == "previous_own_link":
            event = join()
            event["invite_link"] = {"invite_link": INVITE}
            session.add(Inbox(id=10, state="done", payload={"update_id": 10, "chat_member": event}))
    if exemption == "administrator":
        tg.members[(-1001, 42)] = {"status": "administrator"}
    elif exemption == "superadmin":
        settings.superadmin_ids = [1, 42]
    await svc.message(message(10, url))
    await drain(svc)
    assert not any(name in ("ban_chat_member", "delete_message") for name, _ in tg.calls)


async def test_slow_link_check_does_not_hold_database_or_new_member_verification(env, monkeypatch):
    original = await setup(env, monkeypatch)
    _, db, tg, svc = env
    started, release = asyncio.Event(), asyncio.Event()

    async def slow(method, **kwargs):
        if method == "get_chat" and kwargs["chat_id"] == "@other_group":
            started.set()
            await release.wait()
        return await original(method, **kwargs)

    monkeypatch.setattr(tg, "call", slow)
    await svc.message(message(10, "https://t.me/other_group"))
    pending = asyncio.create_task(svc.advertising.run_one())
    try:
        await asyncio.wait_for(started.wait(), 2)
        await asyncio.wait_for(svc.member_event(join(user_id=43)), 2)
        await asyncio.wait_for(svc.run_one_action(include_progress=False), 2)
        assert tg.members[(-1001, 43)]["status"] == "restricted"
        async with db.transaction() as session:
            session.add(Whitelist(group_id=-1001, user_id=42, moderation=True))
    finally:
        release.set()
        await pending
    await drain(svc)
    assert not any(name == "ban_chat_member" for name, _ in tg.calls)


async def test_new_allow_list_cancels_a_confirmed_but_queued_ban(env, monkeypatch):
    await setup(env, monkeypatch)
    _, db, tg, svc = env
    await svc.message(message(10, INVITE))
    await svc.advertising.run_one()
    async with db.transaction() as session:
        group = await session.get(Group, -1001)
        group.settings = {**group.settings, "telegram_allowed_links": [INVITE]}
    await drain(svc)
    assert not any(name == "ban_chat_member" for name, _ in tg.calls)


async def test_known_ad_is_permanently_banned_during_verification_and_old_buttons_fail(env, monkeypatch):
    from test_verification import fixed_challenge, latest

    verification = await fixed_challenge(env, monkeypatch)
    _, db, tg, svc = env
    await svc.message(message(10, "来内部群学习老师的分析逻辑，让每一次开仓都有依据。"))
    await svc.callback({"id": "old-answer", "from": {"id": 42},
                        "data": f"v:{verification.id}:{verification.nonce}:1"})
    await drain(svc)
    assert tg.members[(-1001, 42)] == {"status": "kicked", "until_date": 0}
    assert (await latest(db)).state == "cancelled"


async def test_unknown_public_target_never_becomes_a_guessed_group_ban(env, monkeypatch):
    await setup(env, monkeypatch)
    _, db, tg, svc = env
    await svc.message(message(10, "https://t.me/cannot_resolve"))
    await drain(svc)
    assert not any(name == "ban_chat_member" for name, _ in tg.calls)
    async with db.sessions() as session:
        action = await session.scalar(select(Action).where(Action.kind == "advertisement_check"))
        assert action.state == "succeeded" and action.payload["unresolved"]


async def test_ad_ban_correction_and_bot_allowed_link_editor(env, monkeypatch, client):
    from purrai.runtime import Runtime
    from test_bot_admin import click, deliver, private

    await setup(env, monkeypatch)
    _, db, tg, svc = env
    await svc.message(message(10, INVITE))
    await drain(svc)
    result = (await client.get("/api/groups/-1001/violations")).json()["items"][0]
    response = await client.post(f"/api/groups/-1001/violations/{result['id']}/actions",
                                 json={"action": "unban", "request_id": "correct-ad-ban", "reason": "误判核实"})
    assert response.status_code == 200
    await drain(svc)
    assert tg.members[(-1001, 42)]["status"] == "left"
    runtime = Runtime(db, tg, svc)
    try:
        await runtime.admin.settings_panel(1, -1001, "moderation")
        await click(runtime, tg, "允许的 Telegram 链接")
        await deliver(runtime, {"message": private(INVITE)})
        async with db.sessions() as session:
            assert (await session.get(Group, -1001)).settings["telegram_allowed_links"] == [INVITE]
    finally:
        await runtime.stop()


async def test_ad_and_link_preview_and_allowed_link_validation(client):
    response = await client.post("/api/groups/-1001/rules/preview", json={
        "text": "来内部群学习老师的分析逻辑，让每一次开仓都有依据。"})
    assert response.status_code == 200 and response.json()["ban_member"]
    assert not response.json()["mute_member"]
    response = await client.post("/api/groups/-1001/rules/preview", json={"text": INVITE})
    assert response.status_code == 200 and response.json()["notes"]
    response = await client.patch("/api/groups/-1001/settings", json={"settings": {
        "telegram_allowed_links": [INVITE, INVITE, "https://t.me/news_feed"], "advertising_ban": False}})
    assert response.status_code == 200
    assert response.json()["settings"]["telegram_allowed_links"] == [INVITE, "https://t.me/news_feed"]
    response = await client.patch("/api/groups/-1001/settings", json={"settings": {
        "telegram_allowed_links": ["https://unrelated.example/" ]}})
    assert response.status_code == 422


@pytest.mark.parametrize("applied", [False, True])
async def test_permanent_ad_ban_recovers_after_restart_and_lost_response(env, monkeypatch, applied):
    original = await setup(env, monkeypatch)
    settings, db, tg, svc = env
    lost = False

    async def uncertain(method, **kwargs):
        nonlocal lost
        if method == "ban_chat_member" and not lost:
            lost = True
            if applied:
                await original(method, **kwargs)
            raise ConnectionError("response lost")
        return await original(method, **kwargs)

    monkeypatch.setattr(tg, "call", uncertain)
    await svc.message(message(10, "来内部群学习老师的分析逻辑，让每一次开仓都有依据。"))
    await drain(svc)
    async with db.transaction() as session:
        action = await session.scalar(select(Action).where(Action.kind == "advertisement_ban"))
        action.available_at = 0
    await drain(Service(db, tg, settings))
    assert tg.members[(-1001, 42)] == {"status": "kicked", "until_date": 0}
    assert len([1 for name, _ in tg.calls if name == "ban_chat_member"]) == 1


def test_invite_hash_case_and_non_group_service_links():
    assert parse_link(OWN).target == "OwnInvite123456"
    assert parse_link("https://t.me/share/url?url=https://example.com") is None
    assert parse_link("https://t.me.evil.test/other_group") is None
    assert parse_link("https://evil.test@t.me/other_group") is None


@pytest.mark.parametrize("group_on,invite_on,body,banned", [
    (False, True, INVITE, True), (True, False, INVITE, False),
    (False, True, "https://t.me/other_group", False),
    (True, False, "https://t.me/other_group", True),
    (False, False, INVITE, False),
])
async def test_public_group_and_private_invite_settings_apply_independently(env, monkeypatch, group_on, invite_on, body, banned):
    await setup(env, monkeypatch)
    _, db, tg, svc = env
    async with db.transaction() as session:
        group = await session.get(Group, -1001)
        group.settings = {**group.settings, "external_group_ban": group_on, "external_invite_ban": invite_on}
    await svc.message(message(10, body))
    await drain(svc)
    assert any(name == "ban_chat_member" for name, _ in tg.calls) == banned
