import asyncio

import pytest
from sqlalchemy import select

from conftest import drain, join, message
from purrai.models import Action, Group, Member, Rule, Verification, Violation, Whitelist, now
from test_profile_trust import profile_api
from test_verification import fixed_challenge, latest


async def rules(db):
    async with db.transaction() as session:
        session.add_all(
            [
                Rule(group_id=-1001, kind="keyword", value="广告"),
                Rule(group_id=-1001, kind="keyword", value="推广", scope="name"),
                Rule(group_id=-1001, kind="block_mention", value="badpromo"),
                Rule(group_id=-1001, kind="block_domain", value="spam.example"),
            ]
        )


def promotion(tg, role="administrator"):
    old = {**tg.members.get((-1001, 42), {"status": "member"}), "user": {"id": 42}}
    new = {"status": role, "user": {"id": 42}}
    tg.members[(-1001, 42)] = new
    return {
        "chat": {"id": -1001, "type": "supergroup"},
        "date": int(now()),
        "from": {"id": 1},
        "old_chat_member": old,
        "new_chat_member": new,
    }


@pytest.mark.parametrize("role", ["creator", "administrator"])
@pytest.mark.parametrize(
    "variant",
    [
        "nickname",
        "username",
        "text",
        "caption",
        "hidden_link",
        "contact",
        "photo",
        "sticker",
        "emoji",
        "status",
        "repeat",
        "edited",
    ],
)
async def test_all_message_filters_exempt_current_owner_and_admin(env, monkeypatch, role, variant):
    _, db, tg, svc = env
    await rules(db)
    await profile_api(tg, monkeypatch)
    async with db.transaction() as session:
        session.add(Member(group_id=-1001, user_id=42, joined_at=now(), join_observed=True, passed_at=now()))
    tg.members[(-1001, 42)] = {"status": role}
    incoming = message(1, "普通内容")
    if variant == "nickname":
        incoming["from"]["first_name"] = "推广"
    elif variant == "username":
        incoming["from"]["username"] = "badpromo"
    elif variant in ("text", "edited"):
        incoming["text"] = "广告"
    elif variant == "caption":
        incoming.pop("text")
        incoming.update(photo=[{"file_id": "image"}], caption="广告 https://spam.example")
    elif variant == "hidden_link":
        incoming["entities"] = [
            {"type": "text_link", "offset": 0, "length": 4, "url": "https://spam.example"}
        ]
    elif variant == "contact":
        incoming["contact"] = {"phone_number": "123", "first_name": "test"}
    elif variant == "photo":
        incoming["photo"] = [{"file_id": "image"}]
    elif variant == "sticker":
        incoming["sticker"] = {"file_id": "sticker", "emoji": "🙂"}
    elif variant == "emoji":
        incoming["text"] = "🙂"
    elif variant == "status":
        incoming["from"]["is_premium"] = True
    for identifier in range(1, 7 if variant == "repeat" else 2):
        await svc.message({**incoming, "message_id": identifier}, edited=variant == "edited")
    await drain(svc)
    assert not any(
        method in ("delete_message", "restrict_chat_member", "ban_chat_member", "send_photo")
        for method, _ in tg.calls
    )
    assert not any(method == "get_custom_emoji_stickers" for method, _ in tg.calls)
    async with db.sessions() as session:
        assert (await session.scalar(select(Violation))) is None
        assert (await session.scalar(select(Action))) is None
        assert await session.get(Whitelist, (-1001, 42)) is None


@pytest.mark.parametrize("role", ["creator", "administrator"])
async def test_join_as_administrator_skips_captcha_name_and_status(env, role):
    _, db, tg, svc = env
    await rules(db)
    event = join(new=role)
    event["new_chat_member"]["user"].update(first_name="推广", is_premium=True)
    await svc.member_event(event)
    await drain(svc)
    assert not tg.calls
    async with db.sessions() as session:
        assert await session.scalar(select(Verification)) is None
        assert await session.scalar(select(Violation)) is None


async def test_promotion_cancels_pending_verification_and_deletion_but_demotion_resumes_rules(
    env, monkeypatch
):
    verification = await fixed_challenge(env, monkeypatch)
    _, db, tg, svc = env
    await svc.message(message(10, "尚未验证时的消息"))
    await svc.member_event(promotion(tg), event_id=50)
    tg.calls.clear()
    await drain(svc)
    assert [args["message_id"] for method, args in tg.calls if method == "delete_message"] == [
        verification.message_id
    ]
    assert not any(method in ("restrict_chat_member", "ban_chat_member") for method, _ in tg.calls)
    assert (await latest(db)).state == "passed"
    async with db.sessions() as session:
        assert await session.get(Whitelist, (-1001, 42)) is None
    await svc.member_event(promotion(tg, "member"), event_id=51)
    await rules(db)
    incoming = message(11, "普通消息")
    incoming["from"]["first_name"] = "推广"
    await svc.message(incoming)
    await drain(svc)
    assert tg.members[(-1001, 42)]["until_date"] == 0
    assert any(method == "delete_message" and args["message_id"] == 11 for method, args in tg.calls)


@pytest.mark.parametrize("variant", ["pending_message", "profile", "media", "keyword", "status"])
async def test_queued_tasks_recheck_role_even_without_promotion_update(env, monkeypatch, variant):
    _, db, tg, svc = env
    await rules(db)
    await profile_api(tg, monkeypatch)
    incoming = message(10, "普通消息")
    if variant == "pending_message":
        await fixed_challenge(env, monkeypatch)
    elif variant == "profile":
        incoming["from"]["first_name"] = "推广"
    elif variant == "media":
        async with db.transaction() as session:
            session.add(Member(group_id=-1001, user_id=42, joined_at=now(), join_observed=True))
        incoming["photo"] = [{"file_id": "image"}]
    elif variant == "keyword":
        incoming["text"] = "广告"
    else:
        incoming["from"]["is_premium"] = True
    await svc.message(incoming)
    tg.members[(-1001, 42)] = {"status": "administrator"}
    tg.calls.clear()
    await drain(svc)
    assert not any(
        method in ("restrict_chat_member", "ban_chat_member", "send_photo") for method, _ in tg.calls
    )
    assert not any(method == "delete_message" and args["message_id"] == 10 for method, args in tg.calls)
    async with db.sessions() as session:
        assert not any(action.state == "pending" for action in await session.scalars(select(Action)))


@pytest.mark.parametrize("policy", ["kick", "permanent_mute"])
async def test_timeout_exempts_promoted_admin_without_membership_event(env, monkeypatch, policy):
    await fixed_challenge(env, monkeypatch)
    _, db, tg, svc = env
    async with db.transaction() as session:
        group = await session.get(Group, -1001)
        group.settings = {**group.settings, "verification_failure_action": policy}
        (await session.scalar(select(Verification))).deadline = 0
    await svc.scan_timeouts()
    tg.members[(-1001, 42)] = {"status": "creator"}
    tg.calls.clear()
    await drain(svc)
    assert not any(
        method in ("restrict_chat_member", "ban_chat_member", "unban_chat_member") for method, _ in tg.calls
    )
    assert (await latest(db)).state == "passed"


async def test_progress_exempts_admin_and_invalidates_old_captcha_buttons(env, monkeypatch):
    verification = await fixed_challenge(env, monkeypatch)
    _, db, tg, svc = env
    await svc.callback(
        {
            "id": "digit-before-promotion",
            "from": {"id": 42},
            "data": f"v:{verification.id}:{verification.nonce}:1",
        }
    )
    async with db.transaction() as session:
        (await session.scalar(select(Action).where(Action.kind == "progress"))).available_at = 0
    tg.members[(-1001, 42)] = {"status": "administrator"}
    tg.calls.clear()
    assert await svc.progress.run_one()
    assert not any(method == "edit_message_caption" for method, _ in tg.calls)
    await svc.callback(
        {
            "id": "digit-after-promotion",
            "from": {"id": 42},
            "data": f"v:{verification.id}:{verification.nonce}:2",
        }
    )
    assert (await latest(db)).state == "passed"
    assert (await latest(db)).input_digits == "1"


async def test_promotion_while_role_lookup_is_pending_cancels_stale_deletion(env, monkeypatch):
    _, db, tg, svc = env
    await rules(db)
    await svc.message(message(10, "广告"))
    entered, release = asyncio.Event(), asyncio.Event()
    original = tg.call

    async def slow(method, **kwargs):
        if method == "get_chat_member" and kwargs.get("user_id") == 42:
            entered.set()
            await release.wait()
            return {"status": "member"}
        return await original(method, **kwargs)

    monkeypatch.setattr(tg, "call", slow)
    running = asyncio.create_task(svc.run_one_action())
    try:
        await asyncio.wait_for(entered.wait(), 3)
        await svc.member_event(promotion(tg), event_id=50)
    finally:
        release.set()
        await running
    assert not any(method == "delete_message" for method, _ in tg.calls)


async def test_cannot_confirm_role_keeps_delete_pending_until_retry(env):
    _, db, tg, svc = env
    await rules(db)
    await svc.message(message(10, "广告"))
    tg.members[(-1001, 42)] = {"status": "administrator"}
    tg.fail["get_chat_member"] = 1
    await svc.run_one_action()
    async with db.transaction() as session:
        action = await session.scalar(select(Action).where(Action.kind == "delete"))
        assert action.state == "pending"
        action.available_at = 0
    await drain(svc)
    assert not any(method == "delete_message" for method, _ in tg.calls)
