import time

import pytest
from sqlalchemy import select

from conftest import drain, join, message
from purrai.models import Action, Fingerprint, Group, Member, Rule, Verification, Violation, Whitelist
from purrai.rules import assess, matches
from purrai.runtime import Runtime
from purrai.schemas import GroupSettings
from test_bot_admin import click, deliver, open_group, private


async def add_rule(database, **values):
    async with database.transaction() as session:
        row = Rule(group_id=-1001, kind="keyword", **values)
        session.add(row)


async def set_settings(database, **values):
    async with database.transaction() as session:
        group = await session.get(Group, -1001)
        group.settings = GroupSettings(**{**group.settings, **values}).model_dump()


def named(identifier, body, name="普通成员", username=""):
    item = message(identifier, body)
    item["from"].update(first_name=name, username=username)
    return item


@pytest.mark.parametrize(
    "mode,name", [("contains", "同城推广"), ("compact", "同💚-城推广"), ("pinyin", "童城推广")]
)
async def test_profile_rule_permanently_mutes_and_deletes_message(env, mode, name):
    _, database, telegram, service = env
    await add_rule(database, value="同城", scope="name", mode=mode)
    await service.message(named(100, "大家好", name))
    await drain(service)
    assert telegram.members[(-1001, 42)]["until_date"] == 0
    assert not telegram.members[(-1001, 42)]["can_send_messages"]
    assert not any(method in ("ban_chat_member", "send_message") for method, _ in telegram.calls)
    assert any(method == "delete_message" and data["message_id"] == 100 for method, data in telegram.calls)
    async with database.sessions() as session:
        row = await session.scalar(select(Violation))
        assert row.profile_violation and row.message_violation and not row.warning_active


@pytest.mark.parametrize(
    "mode,body", [("contains", "同城推广"), ("compact", "同💚-城推广"), ("pinyin", "童城推广")]
)
async def test_body_rules_only_delete_even_after_many_violations(env, mode, body):
    _, database, telegram, service = env
    await add_rule(database, value="同城", scope="message", mode=mode)
    for identifier in range(5):
        await service.message(named(identifier, body))
    await drain(service)
    assert len([1 for method, _ in telegram.calls if method == "delete_message"]) == 5
    assert not any(
        method in ("restrict_chat_member", "ban_chat_member", "send_message") for method, _ in telegram.calls
    )
    async with database.sessions() as session:
        assert all(not row.warning_active for row in (await session.scalars(select(Violation))).all())


async def test_name_and_body_are_separate_and_combination_requires_both(env):
    _, database, telegram, service = env
    await add_rule(database, value="同城", scope="name_and_message", context_value="面付", mode="compact")
    await service.message(named(1, "正常讨论", "同💚城"))
    await service.message(named(2, "面付", "普通成员"))
    await drain(service)
    assert not any(method in ("delete_message", "restrict_chat_member") for method, _ in telegram.calls)
    await service.message(named(3, "可面💚付", "同💚城"))
    await drain(service)
    assert any(method == "delete_message" and data["message_id"] == 3 for method, data in telegram.calls)
    assert not telegram.members[(-1001, 42)]["can_send_messages"]


async def test_profile_is_checked_on_join_even_with_captcha_disabled(env):
    _, database, telegram, service = env
    await set_settings(database, verification_enabled=False)
    await add_rule(database, value="推广", scope="name")
    incoming = join()
    incoming["new_chat_member"]["user"]["first_name"] = "推广账号"
    await service.member_event(incoming)
    await drain(service)
    assert not telegram.members[(-1001, 42)]["can_send_messages"]
    assert not any(method in ("delete_message", "send_photo") for method, _ in telegram.calls)
    async with database.sessions() as session:
        assert not (await session.scalars(select(Verification))).all()


async def test_username_blacklist_handles_sender_mentions_and_tme_link_independently(env):
    _, database, telegram, service = env
    async with database.transaction() as session:
        session.add(Rule(group_id=-1001, kind="block_mention", value="@badpromo"))
    await service.message(named(1, "正常内容", username="badpromo"))
    await drain(service)
    assert not telegram.members[(-1001, 42)]["can_send_messages"]
    assert any(method == "delete_message" and data["message_id"] == 1 for method, data in telegram.calls)
    await service.message(message(2, "联系 @badpromo", user_id=43))
    await service.message(message(3, "https://t.me/badpromo", user_id=43))
    await drain(service)
    assert len([1 for method, _ in telegram.calls if method == "delete_message"]) == 3
    assert not any(
        method == "restrict_chat_member" and data["user_id"] == 43 for method, data in telegram.calls
    )


async def test_first_messages_remain_under_observation_after_time_window_and_edits_do_not_inflate_count(env):
    _, database, telegram, service = env
    async with database.transaction() as session:
        session.add(Member(group_id=-1001, user_id=42, passed_at=time.time() - 4 * 86400))
    for identifier in (1, 2):
        await service.message(named(identifier, "正常讨论"))
    await service.message(named(1, "编辑后的正常内容"), edited=True)
    await service.message(named(3, "联系 @unknown_user"))
    await service.message(named(4, "https://example.com"))
    await service.message(named(1, "https://example.com"), edited=True)
    await drain(service)
    deleted = [data["message_id"] for method, data in telegram.calls if method == "delete_message"]
    assert sorted(deleted) == [1, 3]
    async with database.sessions() as session:
        assert (await session.get(Member, (-1001, 42))).observed_messages == 4


async def test_mention_allow_list_does_not_exempt_keywords_or_similar_email_text(env):
    _, database, telegram, service = env
    async with database.transaction() as session:
        session.add(Member(group_id=-1001, user_id=42, passed_at=time.time()))
        session.add(Rule(group_id=-1001, kind="allow_mention", value="helper"))
        session.add(Rule(group_id=-1001, kind="keyword", value="广告"))
    await service.message(named(1, "请问 @helper"))
    await service.message(named(2, "邮箱 name@example.com"))
    await service.message(named(3, "广告 @helper"))
    await drain(service)
    assert [data["message_id"] for method, data in telegram.calls if method == "delete_message"] == [3]


async def test_similar_spam_ignores_emoji_variation_and_never_mutes_member(env):
    _, database, telegram, service = env
    text = "欢迎了解今天的活动详情查看介绍获取更多信息参与我们正在举行的主题活动"
    for identifier, emoji in enumerate(("🎁", "🌟", "🎈", "✨")):
        await service.message(named(identifier, text + emoji))
    await drain(service)
    assert [data["message_id"] for method, data in telegram.calls if method == "delete_message"] == [3]
    assert not any(method in ("restrict_chat_member", "ban_chat_member") for method, _ in telegram.calls)
    async with database.sessions() as session:
        assert "近似内容刷屏" in (await session.scalar(select(Violation))).reasons
        assert all(
            len(row.similarity_hash) == 16 for row in (await session.scalars(select(Fingerprint))).all()
        )


async def test_burst_detection_counts_nontext_messages_and_treats_album_as_one(env):
    _, database, telegram, service = env
    await set_settings(database, burst_count=3)
    for identifier in range(10):
        await service.message(message(identifier, "", media_group_id="album-1"))
    await service.message(message(10, "", media_group_id="album-2"))
    await drain(service)
    assert not any(method == "delete_message" for method, _ in telegram.calls)
    await service.message(message(11, ""))
    await drain(service)
    assert [data["message_id"] for method, data in telegram.calls if method == "delete_message"] == [11]


async def test_old_generation_and_old_messages_do_not_trigger_new_burst(env):
    _, database, telegram, service = env
    await set_settings(database, verification_enabled=False, burst_count=3)
    await service.member_event(join(), event_id=1)
    await service.message(named(1, "第一条"))
    await service.message(named(2, "第二条"))
    await service.member_event(join(old="member", new="left"), event_id=2)
    await service.member_event(join(), event_id=3)
    await service.message(named(3, "重新加入后"))
    await drain(service)
    assert not any(method == "delete_message" for method, _ in telegram.calls)


async def test_whitelist_and_admin_skip_profile_filter_and_old_penalty_is_cancelled(env):
    _, database, telegram, service = env
    await add_rule(database, value="推广", scope="either")
    async with database.transaction() as session:
        session.add(Whitelist(group_id=-1001, user_id=42, moderation=True))
        session.add(
            Action(
                id="legacy",
                kind="moderation_mute",
                dedupe_key="legacy",
                group_id=-1001,
                user_id=43,
                payload={},
            )
        )
    telegram.members[(-1001, 43)] = {"status": "administrator"}
    await service.message(named(1, "推广", "推广"))
    item = named(2, "推广", "推广")
    item["from"]["id"] = 43
    await service.message(item)
    await drain(service)
    assert not any(method in ("delete_message", "restrict_chat_member") for method, _ in telegram.calls)
    async with database.sessions() as session:
        assert (await session.get(Action, "legacy")).state == "cancelled"


async def test_bot_can_configure_name_pinyin_rules_and_preview_actions(env):
    _, database, telegram, service = env
    runtime = Runtime(database, telegram, service)
    await open_group(runtime, telegram)
    await click(runtime, telegram, "广告规则与测试")
    await click(runtime, telegram, "添加关键词")
    await deliver(runtime, {"message": private("同城")})
    await click(runtime, telegram, "关键词 · 同城")
    await click(runtime, telegram, "范围改为昵称及用户名")
    await click(runtime, telegram, "改为完整匹配")
    await click(runtime, telegram, "改为忽略表情和分隔符匹配")
    await click(runtime, telegram, "改为谐音匹配")
    async with database.sessions() as session:
        row = await session.scalar(select(Rule))
        assert row.scope == "name" and row.mode == "pinyin"
    await click(runtime, telegram, "返回列表")
    await click(runtime, telegram, "测试当前规则")
    await deliver(runtime, {"message": private("昵称：童🌷城\n大家好")})
    response = next(data["text"] for method, data in reversed(telegram.calls) if method == "send_message")
    assert "将禁言：是" in response and "将删消息：是" in response


def test_pinyin_and_compact_are_explicit_modes_and_short_homophones_are_rejected():
    from pydantic import ValidationError
    from purrai.schemas import RuleInput

    assert not matches("童城", "同城", "contains")
    assert matches("童🌷城", "同城", "pinyin")
    assert matches("推💚-广", "推广", "compact")
    with pytest.raises(ValidationError):
        RuleInput(kind="keyword", value="同", mode="pinyin")
    result = assess(
        "正常内容",
        [],
        [{"kind": "keyword", "value": "同城", "scope": "name", "mode": "pinyin"}],
        GroupSettings().model_dump(),
        sender_name="童城",
    )
    assert result["name_reasons"] and not result["reasons"]


async def test_channel_name_hit_deletes_message_without_calling_member_mute(env):
    _, database, telegram, service = env
    await add_rule(database, value="推广", scope="name")
    await service.message(message(1, "正常文字", sender_chat={"id": -2002, "title": "推广频道"}))
    await drain(service)
    assert any(method == "delete_message" for method, _ in telegram.calls)
    assert not any(method in ("restrict_chat_member", "ban_chat_member") for method, _ in telegram.calls)


async def test_profile_mute_can_be_released_without_affecting_original_group_permissions(env):
    _, database, telegram, service = env
    await add_rule(database, value="推广", scope="name")
    telegram.defaults["can_send_photos"] = False
    await service.message(named(1, "正常文字", "推广昵称"))
    await drain(service)
    runtime = Runtime(database, telegram, service)
    await runtime.management.member_action(1, -1001, 42, "unmute", "release-profile-mute", "核实后解除")
    await drain(service)
    assert telegram.members[(-1001, 42)]["can_send_messages"]
    assert not telegram.members[(-1001, 42)]["can_send_photos"]


async def test_http_rule_preview_distinguishes_name_mute_and_message_deletion_and_three_day_settings(client):
    response = await client.post(
        "/api/groups/-1001/rules",
        json={"kind": "keyword", "value": "同城", "mode": "pinyin", "scope": "name"},
    )
    assert response.status_code == 200
    response = await client.post(
        "/api/groups/-1001/rules/preview", json={"text": "大家好", "sender_name": "童城"}
    )
    assert response.status_code == 200
    assert response.json()["mute_member"] and response.json()["delete_message"]
    response = await client.post(
        "/api/groups/-1001/rules", json={"kind": "keyword", "value": "广告", "scope": "message"}
    )
    assert response.status_code == 200
    response = await client.post(
        "/api/groups/-1001/rules/preview", json={"text": "广告", "sender_name": "小明"}
    )
    assert response.json()["delete_message"] and not response.json()["mute_member"]
    response = await client.patch("/api/groups/-1001/settings", json={"settings": {"mute_seconds": 259200}})
    assert response.status_code == 200 and response.json()["settings"]["mute_seconds"] == 259200
    response = await client.post(
        "/api/groups/-1001/rules", json={"kind": "keyword", "value": "同城", "scope": "name_and_message"}
    )
    assert response.status_code == 422


def test_chinese_adjacent_mentions_do_not_bypass_filter_but_emails_are_not_mentions():
    from purrai.rules import mentions

    assert mentions("联系@badpromo，或者＠BAdPromo") == ["badpromo"]
    assert mentions("邮箱 alice@example.com 和 张三@example.com") == []


async def test_short_numeric_messages_do_not_become_long_enough_for_similarity_detection(env):
    _, database, telegram, service = env
    for identifier, body in enumerate(("1 2 3", "4 5 6", "7 8 9", "9 8 7")):
        await service.message(named(identifier, body))
    await drain(service)
    assert not any(method == "delete_message" for method, _ in telegram.calls)
