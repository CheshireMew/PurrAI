"""Saved destination rules match the actual sender's biography before any AI call."""
import pytest
from sqlalchemy import select

from conftest import drain, message
from purrai.models import Action, AdvertisementReview, ChatTurn, Group, Member, now
from purrai.runtime import Runtime
from purrai.rules import assess
from purrai.shared_policy import effective_rules
from test_bio_advertising import profiles


async def save_target(client, value, penalty="ban"):
    response = await client.post("/api/groups/-1001/rules", json={
        "kind": "block_target", "value": value, "mode": "exact", "penalty": penalty},
        headers={"Idempotency-Key": "bio-target-create"})
    assert response.status_code == 200, response.text
    return response.json()


async def test_target_create_replace_disable_controls_bio_and_body_through_management(client, env):
    _, db, _, _ = env
    first, second = "https://t.me/+ArbitraryInvite123", "https://t.me/+ReplacementInvite45"
    rule = await save_target(client, first)

    async def verdict(bio):
        async with db.sessions() as session:
            group = await session.get(Group, -1001)
            return assess("普通正文", [], await effective_rules(session, -1001), group.settings, sender_bio=bio)

    assert (await verdict(first))["bio_reasons"]
    assert not (await verdict(second))["bio_reasons"]
    preview = await client.post("/api/groups/-1001/rules/preview", json={"text": "普通正文", "sender_bio": first})
    assert preview.status_code == 200 and preview.json()["ban_member"]
    update = {key: rule[key] for key in ("kind", "value", "mode", "scope", "penalty", "enabled")}
    update["value"] = second
    response = await client.put(f"/api/groups/-1001/rules/{rule['id']}", json=update,
        headers={"If-Match": rule["version"], "Idempotency-Key": "bio-target-replace"})
    assert response.status_code == 200, response.text
    rule = response.json()
    assert not (await verdict(first))["bio_reasons"]
    assert (await verdict(second))["bio_reasons"]
    response = await client.put(f"/api/groups/-1001/rules/{rule['id']}", json={**update, "enabled": False},
        headers={"If-Match": rule["version"], "Idempotency-Key": "bio-target-disable"})
    assert response.status_code == 200, response.text
    assert not (await verdict(second))["bio_reasons"]


@pytest.mark.parametrize("penalty,effect", [("delete", "delete_message"),
    ("permanent_mute", "restrict_chat_member"), ("ban", "ban_chat_member")])
async def test_bio_target_is_local_and_prevents_model_or_chat_requests(client, env, penalty, effect):
    settings, db, tg, service = env
    settings.chat_api_key = "offline-test"
    target = "https://t.me/+GenericBlocked1234"
    profiles(tg, {42: "个人记录 " + target})
    await save_target(client, target, penalty)
    async with db.transaction() as session:
        group = await session.get(Group, -1001)
        group.settings = {**group.settings, "chat_enabled": True, "chat_proactive": True}
        session.add(Member(group_id=-1001, user_id=42, joined_at=now() - 86400 * 30, present=True))
    runtime = Runtime(db, tg, service)
    try:
        from aiogram.types import Message
        await runtime.events.message(Message.model_validate(message(901, "@PurrAITestBot 帮我解释一下今天的话题",
            **{"from": {"id": 42, "is_bot": False, "first_name": "成员"}})))
        async with db.sessions() as session:
            review = await session.scalar(select(AdvertisementReview).where(AdvertisementReview.message_id == 901))
            assert review and review.state == "handled" and review.result["source"] == "rules"
            assert not (await session.scalars(select(ChatTurn).where(ChatTurn.state.in_(("pending", "generating", "sending"))))).all()
            assert all(row.payload["message_evidence"]["bio_dependent"] for row in await session.scalars(
                select(Action).where(Action.kind.in_(("delete", "rule_mute", "profile_mute", "advertisement_ban")))))
        assert not await service.advertisement_review.run_one()
        await drain(service)
        assert any(method == effect for method, _ in tg.calls)
        assert any(method == "delete_message" and data["message_id"] == 901 for method, data in tg.calls)
    finally:
        await runtime.admin.ui.close()
        await runtime.chat.close()


@pytest.mark.parametrize("change", ["profile", "rule"])
async def test_pending_bio_target_penalty_rechecks_profile_and_current_rule(client, env, change):
    _, db, tg, service = env
    target = "https://t.me/+GenericBlocked5678"
    values = {42: target}
    profiles(tg, values)
    rule = await save_target(client, target)
    await service.message(message(902, "普通正文"))
    if change == "profile":
        values[42] = "普通简介"
    else:
        response = await client.put(f"/api/groups/-1001/rules/{rule['id']}",
            json={**rule, "enabled": False},
            headers={"If-Match": rule["version"], "Idempotency-Key": "disable-pending-target"})
        assert response.status_code == 200, response.text
    await drain(service)
    assert not any(method in ("delete_message", "ban_chat_member", "restrict_chat_member") for method, _ in tg.calls)


def test_only_configured_targets_and_enabled_bio_checks_apply():
    rules = [{"kind": "block_target", "value": "invite:GenericBlocked5678", "penalty": "ban", "enabled": True}]
    settings = {"bio_advertising_enabled": True, "block_invites": True}
    assert not assess("日常聊天", [], rules, settings, sender_bio="https://t.me/+UnrelatedInvite123")["bio_reasons"]
    assert not assess("日常聊天", [], rules, {**settings, "bio_advertising_enabled": False},
        sender_bio="https://t.me/+GenericBlocked5678")["bio_reasons"]
