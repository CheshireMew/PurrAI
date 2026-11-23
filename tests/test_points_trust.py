import asyncio
from datetime import date, timedelta

import pytest
from aiogram.types import Message

from conftest import drain, message
from purrai.models import Action, AdvertisementReview, Group, Member, PointsDaily, Violation, now
from purrai.points_rewards import credit, user_account
from purrai.points_trust import member_trust
from purrai.runtime import Runtime
from test_advertisement_review import records, reviewer
from test_points_bot import configure, flush, user_message


async def contribution(env, points, *, user_id=42, days=10, kind="chat", group_id=-1001):
    async with env[1].transaction() as session:
        account = await user_account(session, group_id, {"id": user_id, "first_name": "群友"})
        await credit(session, account, points, f"earned:{kind}:{user_id}:{account.balance}:{points}", kind,
                     "测试历史积分来源", user_id)
        for offset in range(days):
            day = (date(2026, 11, 1) + timedelta(days=offset)).isoformat()
            if not await session.get(PointsDaily, (group_id, user_id, day)):
                session.add(PointsDaily(group_id=group_id, user_id=user_id, day=day, chat_points=1))
        return account.id


@pytest.mark.parametrize("points,days,level", [(49, 0, 0), (50, 0, 1), (99, 0, 1), (100, 0, 2), (100, 6, 2)])
async def test_points_privilege_thresholds_need_no_minimum_active_days(env, client, points, days, level):
    await configure(client)
    await contribution(env, points, days=days)
    async with env[1].sessions() as session:
        trust = await member_trust(session, -1001, 42)
        assert (trust.earned_points, trust.active_days, trust.level) == (points, days, level)
        assert (await member_trust(session, -1001, 43)).level == 0


@pytest.mark.parametrize("kind", ["signin", "packet_claim", "packet_refund", "adjustment", "import"])
async def test_historical_points_including_signin_count_towards_peak_balance(env, client, kind):
    await configure(client)
    await contribution(env, 1000, kind=kind)
    async with env[1].sessions() as session:
        assert (await member_trust(session, -1001, 42)).level == 2


async def test_spending_and_identity_rebinding_do_not_transfer_earned_privileges(env, client):
    await configure(client)
    account_id = await contribution(env, 500)
    async with env[1].transaction() as session:
        from purrai.models import PointsAccount
        account = await session.get(PointsAccount, account_id)
        await credit(session, account, -500, "spent-all", "packet_send", "发出积分红包", 42)
        assert (await member_trust(session, -1001, 42)).advertising
        account.user_id = 43
        assert not (await member_trust(session, -1001, 43)).advertising
        assert not (await member_trust(session, -1001, 42)).advertising


async def test_historical_signin_then_spending_retains_privilege_without_new_earning(env, client):
    await configure(client)
    account_id = await contribution(env, 100, days=0, kind="signin")
    async with env[1].transaction() as session:
        from purrai.models import PointsAccount
        account = await session.get(PointsAccount, account_id)
        await credit(session, account, -100, "spent-signin-points", "packet_send", "消费积分", 42)
    async with env[1].sessions() as session:
        trust = await member_trust(session, -1001, 42)
        assert trust.earned_points == 100 and trust.advertising and trust.newcomer


async def test_cumulative_rewards_below_historical_balance_threshold_do_not_grant_extra_level(env, client):
    await configure(client)
    account_id = await contribution(env, 50, days=0, kind="signin")
    async with env[1].transaction() as session:
        from purrai.models import PointsAccount
        account = await session.get(PointsAccount, account_id)
        await credit(session, account, -50, "spent-first-50", "packet_send", "消费积分", 42)
        await credit(session, account, 50, "second-50", "signin", "签到", 42)
        trust = await member_trust(session, -1001, 42)
        assert trust.earned_points == 50 and trust.newcomer and not trust.advertising


async def test_saved_threshold_changes_and_revocation_apply_without_changing_the_ledger(env, client):
    await configure(client)
    await contribution(env, 500)
    path = "/api/groups/-1001/points/settings"
    async def save(**changes):
        doc = (await client.get(path)).json()
        doc["config"].update(changes)
        result = await client.put(path, json=doc, headers={"Idempotency-Key": f"trust-settings-{doc['version']}"})
        assert result.status_code == 200, result.text

    await save(advertising_exemption_points=600)
    async with env[1].sessions() as session:
        assert (await member_trust(session, -1001, 42)).level == 1
    await save(advertising_exemption_points=400)
    async with env[1].sessions() as session:
        assert (await member_trust(session, -1001, 42)).level == 2
    await save(trust_excluded_user_ids=[42])
    async with env[1].sessions() as session:
        assert (await member_trust(session, -1001, 42)).level == 0
    page = (await client.get("/api/groups/-1001/points")).json()
    assert page["items"][0]["trust"] == {"earned_points": 500, "active_days": 10, "level": 0}
    await save(trust_excluded_user_ids=[], trust_enabled=False)
    async with env[1].sessions() as session:
        assert (await member_trust(session, -1001, 42)).level == 0


async def test_high_points_skip_advertising_model_and_sample_matching_before_dispatch(env, client, monkeypatch):
    await configure(client)
    await contribution(env, 500)
    worker, model = await reviewer(env)
    runtime = Runtime(env[1], env[2], env[3])
    def unexpected(*args, **kwargs):
        pytest.fail("高积分成员不应进入广告样本匹配")
    monkeypatch.setattr("purrai.advertisement_review.rank", unexpected)
    try:
        await runtime.events.message(Message.model_validate(user_message(71, "青岚软件支持生成报表，欢迎交流合作")))
        assert not await worker.run_one()
        review, = await records(env[1], AdvertisementReview)
        assert review.state == "exempt" and review.result["source"] == "points" and review.attempts == 0
        assert not model.calls and not await records(env[1], Violation)
        await flush(env[3], env[1])
        async with env[1].sessions() as session:
            assert (await member_trust(session, -1001, 42)).earned_points == 501
    finally:
        await runtime.stop()


async def test_low_stage_removes_newcomer_media_and_link_limits_but_still_uses_ad_review(env, client):
    await configure(client)
    await contribution(env, 50)
    worker, model = await reviewer(env, lambda _: False)
    async with env[1].transaction() as session:
        session.add(Member(group_id=-1001, user_id=42, joined_at=now(), present=True, join_observed=True))
        group = await session.get(Group, -1001)
        group.settings = {**group.settings, "external_group_ban": False, "external_invite_ban": False}
    await env[3].message(message(72, "", photo=[{"file_id": "photo"}]))
    await env[3].message(message(73, "本次合作说明 https://example.test", entities=[]))
    assert not await records(env[1], Violation)
    assert await worker.run_one()
    assert len(model.calls) == 1
    assert (await records(env[1], AdvertisementReview))[0].state == "clean"


async def test_crossing_threshold_during_model_request_exempts_pending_result(env, client):
    await configure(client)
    await contribution(env, 99)
    worker, model = await reviewer(env)
    model.wait = True
    await env[3].message(message(74, "本条测试等待中的推广审核"))
    task = asyncio.create_task(worker.run_one())
    try:
        await asyncio.wait_for(model.entered.wait(), 5)
        await contribution(env, 1)
    finally:
        model.release.set()
        await task
    review, = await records(env[1], AdvertisementReview)
    assert review.state == "exempt" and not await records(env[1], Violation)


async def test_queued_ai_penalties_cancel_after_member_qualifies(env, client):
    await configure(client)
    worker, _ = await reviewer(env)
    async with env[1].transaction() as session:
        group = await session.get(Group, -1001)
        group.settings = {**group.settings, "ai_ad_limit": 1}
    await env[3].message(message(75, "测试人工贡献达标前排队的推广消息"))
    assert await worker.run_one()
    await contribution(env, 500)
    await drain(env[3])
    assert not any(method in ("delete_message", "restrict_chat_member") for method, _ in env[2].calls)
    actions = [row for row in await records(env[1], Action) if row.kind in ("delete", "ai_ad_mute")]
    assert actions and all(row.state == "cancelled" for row in actions)


async def test_privileges_are_scoped_to_the_group_and_pending_verification_still_applies(env, client):
    from conftest import join
    await configure(client)
    await contribution(env, 500)
    async with env[1].transaction() as session:
        session.add(Group(id=-1002, title="另一群", approved=True, enabled=True))
        assert not (await member_trust(session, -1002, 42)).advertising
    await env[3].member_event(join())
    await drain(env[3])
    await env[3].message(message(76, "验证尚未结束的发言"))
    await drain(env[3])
    assert any(method == "delete_message" and data["message_id"] == 76 for method, data in env[2].calls)
