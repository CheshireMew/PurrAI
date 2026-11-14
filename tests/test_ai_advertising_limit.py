import pytest
from sqlalchemy import select

from conftest import drain, message
from purrai import chat_commands
from purrai import ai_advertising
from purrai.ai_advertising import advertisement_count, record_ai_advertisement
from purrai.chat_model import ChatReply
from purrai.models import Action, ChatMessage, Group, Member, Rule, RuleHit, Violation, Whitelist
from purrai.service import Service
from test_chat import age_reply, configured


async def test_three_actual_model_verdicts_within_window_persist_and_permanently_mute(env, monkeypatch):
    chat, model = await configured(env)
    model.result = ChatReply(should_reply=False, is_advertisement=True)
    instant = [chat_commands.now()]
    monkeypatch.setattr(chat_commands, "now", lambda: instant[0])
    for identifier in (1, 2, 3):
        await chat.observe(message(identifier, f"这是一段交给模型判断的推广内容{identifier}"))
        assert await chat.run_one()
        async with chat.db.sessions() as session:
            assert await advertisement_count(session, -1001, 42) == identifier
        await age_reply(chat)
        instant[0] += 1801
        if identifier < 3:
            assert not any(method == "restrict_chat_member" for method, _ in chat.tg.calls)
    assert len(model.calls) == 3
    restored = Service(chat.db, chat.tg, env[0])
    await drain(restored)
    assert chat.tg.members[(-1001, 42)]["until_date"] == 0
    assert not chat.tg.members[(-1001, 42)]["can_send_messages"]
    assert not any(method in ("delete_message", "ban_chat_member") for method, _ in chat.tg.calls)
    async with chat.db.sessions() as session:
        assert (await session.scalar(select(Action).where(Action.kind == "ai_ad_mute"))).state == "succeeded"


async def test_cached_ad_and_duplicate_model_result_do_not_add_judgments(env):
    chat, model = await configured(env)
    model.result = ChatReply(should_reply=False, is_advertisement=True)
    text = "这是一段交给模型判断的推广内容"
    await chat.observe(message(1, text))
    assert await chat.run_one()
    await chat.observe(message(2, text))
    assert not await chat.run_one()
    async with chat.db.transaction() as session:
        group = await session.get(Group, -1001)
        await record_ai_advertisement(session, group, 42, 1, text, "成员")
        assert await advertisement_count(session, -1001, 42) == 1
    assert len(model.calls) == 1


@pytest.mark.parametrize("exemption", ["administrator", "whitelist", "disabled", "off"])
async def test_ai_limit_respects_existing_protections_and_current_switch(env, exemption):
    _, db, tg, svc = env
    async with db.transaction() as session:
        group = await session.get(Group, -1001)
        if exemption == "whitelist":
            session.add(Whitelist(group_id=-1001, user_id=42, moderation=True))
        if exemption == "disabled":
            group.enabled = False
        for identifier in (1, 2, 3):
            await record_ai_advertisement(session, group, 42, identifier, "模型判定的广告内容", "成员")
        if exemption == "off":
            group.settings = {**group.settings, "chat_advertiser_cooldown": False}
    if exemption == "administrator":
        tg.members[(-1001, 42)] = {"status": "administrator"}
    await drain(svc)
    assert not any(method in ("restrict_chat_member", "ban_chat_member", "delete_message") for method, _ in tg.calls)


async def test_ai_counters_are_per_group_and_disabled_mode_does_not_count(env):
    chat, model = await configured(env, chat_advertiser_cooldown=False)
    model.result = ChatReply(should_reply=False, is_advertisement=True)
    await chat.observe(message(1, "这是一段交给模型判断的推广内容"))
    assert await chat.run_one()
    async with chat.db.sessions() as session:
        assert await advertisement_count(session, -1001, 42) == 0
        assert await advertisement_count(session, -1002, 42) == 0


async def test_two_judgments_in_one_group_and_one_in_another_do_not_mute(env):
    _, db, tg, _ = env
    async with db.transaction() as session:
        first = await session.get(Group, -1001)
        second = Group(id=-1002, approved=True, enabled=True, settings=first.settings)
        session.add(second)
        await session.flush()
        await record_ai_advertisement(session, first, 42, 1, "广告甲", "成员")
        await record_ai_advertisement(session, first, 42, 2, "广告乙", "成员")
        await record_ai_advertisement(session, second, 42, 1, "广告丙", "成员")
        assert await advertisement_count(session, -1001, 42) == 2
        assert await advertisement_count(session, -1002, 42) == 1
        assert await session.scalar(select(Action)) is None


async def test_reenabling_after_cancelled_threshold_waits_for_a_new_actual_verdict(env):
    _, db, tg, svc = env
    async with db.transaction() as session:
        group = await session.get(Group, -1001)
        for identifier in (1, 2, 3):
            await record_ai_advertisement(session, group, 42, identifier, "广告内容", "成员")
        group.settings = {**group.settings, "chat_advertiser_cooldown": False}
    await drain(svc)
    assert not any(method == "restrict_chat_member" for method, _ in tg.calls)
    async with db.transaction() as session:
        group = await session.get(Group, -1001)
        group.settings = {**group.settings, "chat_advertiser_cooldown": True}
        await record_ai_advertisement(session, group, 42, 4, "新的广告内容", "成员")
    await drain(svc)
    assert tg.members[(-1001, 42)]["until_date"] == 0


async def test_three_day_window_expires_each_old_judgment_individually(env, monkeypatch):
    _, db, _, _ = env
    instant = [ai_advertising.now()]
    monkeypatch.setattr(ai_advertising, "now", lambda: instant[0])
    async with db.transaction() as session:
        group = await session.get(Group, -1001)
        await record_ai_advertisement(session, group, 42, 1, "广告甲", "成员")
    instant[0] += 2 * 86400
    async with db.transaction() as session:
        group = await session.get(Group, -1001)
        await record_ai_advertisement(session, group, 42, 2, "广告乙", "成员")
        assert await advertisement_count(session, -1001, 42) == 2
    instant[0] += 86400
    async with db.transaction() as session:
        assert await advertisement_count(session, -1001, 42) == 1
        group = await session.get(Group, -1001)
        await record_ai_advertisement(session, group, 42, 3, "广告丙", "成员")
        assert await advertisement_count(session, -1001, 42) == 2
        assert await session.scalar(select(Action)) is None
    instant[0] += ai_advertising.WINDOW
    async with db.sessions() as session:
        assert await advertisement_count(session, -1001, 42) == 0


async def test_expired_threshold_cancels_penalty_waiting_in_queue(env, monkeypatch):
    _, db, tg, svc = env
    instant = [ai_advertising.now()]
    monkeypatch.setattr(ai_advertising, "now", lambda: instant[0])
    async with db.transaction() as session:
        group = await session.get(Group, -1001)
        for identifier in (1, 2, 3):
            await record_ai_advertisement(session, group, 42, identifier, "广告内容", "成员")
    instant[0] += ai_advertising.WINDOW
    await drain(svc)
    assert not any(method == "restrict_chat_member" for method, _ in tg.calls)
    async with db.sessions() as session:
        assert (await session.scalar(select(Action).where(Action.kind == "ai_ad_mute"))).state == "cancelled"


async def save_ai_keyword(client, value=None, mode=None, previous=None, **changes):
    baseline = (await client.get("/api/settings/defaults")).json()
    draft = {"kind": "keyword", "value": "纸飞机", "mode": "pinyin", "scope": "message",
             "penalty": "ai_confirmed_mute", "enabled": True, **(previous or {}), **changes}
    if value is not None:
        draft["value"] = value
    if mode is not None:
        draft["mode"] = mode
    from uuid import uuid4
    response = await client.patch("/api/settings/defaults", json={
        "rules": [draft], "baseline": baseline, "request_id": uuid4().hex})
    assert response.status_code == 200, response.text
    return response.json()["rules"][0]


@pytest.mark.parametrize("word,text", [
    ("主页", "要是早点遇到我住页博主就好了。 每天打一百多u都有盼头了"),
    ("网赚", "我有个网賺方法可以介绍"),
    ("纸飞机", "这段纸飞机内容需要判断"),
])
async def test_keyword_and_first_actual_ai_verdict_delete_and_mute_once(env, client, word, text):
    chat, model = await configured(env)
    rule = await save_ai_keyword(client, word)
    model.result = ChatReply(should_reply=False, is_advertisement=True)
    incoming = message(201, text)
    # The normal moderation path must leave this candidate for the model.
    await env[3].message(incoming)
    await drain(env[3])
    assert not any(method in ("delete_message", "restrict_chat_member") for method, _ in chat.tg.calls)
    await chat.observe(incoming)
    assert await chat.run_one() and len(model.calls) == 1
    async with chat.db.transaction() as session:
        group = await session.get(Group, -1001)
        await record_ai_advertisement(session, group, 42, 201, text, "成员")
        assert await advertisement_count(session, -1001, 42) == 1
        assert (await session.scalar(select(Violation))).message_violation
        assert (await session.scalar(select(RuleHit))).rule_id == rule["id"]
    restored = Service(chat.db, chat.tg, env[0])
    await drain(restored)
    await drain(restored)
    assert [data["message_id"] for method, data in chat.tg.calls if method == "delete_message"] == [201]
    assert sum(method == "restrict_chat_member" for method, _ in chat.tg.calls) == 1
    assert chat.tg.members[(-1001, 42)]["until_date"] == 0
    assert not chat.tg.members[(-1001, 42)]["can_send_messages"]
    assert not any(method == "ban_chat_member" for method, _ in chat.tg.calls)
    assert any("AI确认广告且命中关键词" in data.get("text", "")
               for method, data in chat.tg.calls if method == "send_message")


@pytest.mark.parametrize("verdict,word,text", [
    (False, "主页", "柴郡喵，我的主页打不开了"),
    (False, "主页", "柴郡喵，有人让我看他主页，是不是广告？"),
    (True, "纸飞机", "柴郡喵，这条没有设置的词"),
])
async def test_either_condition_alone_never_uses_immediate_penalty(env, client, verdict, word, text):
    chat, model = await configured(env)
    await save_ai_keyword(client, word)
    model.result = ChatReply(should_reply=False, is_advertisement=verdict)
    await chat.observe(message(202, text))
    assert await chat.run_one() and len(model.calls) == 1
    await drain(env[3])
    assert not any(method in ("delete_message", "restrict_chat_member", "ban_chat_member") for method, _ in chat.tg.calls)


async def test_ai_keyword_management_edit_and_disable_control_matching(env, client):
    await configured(env)
    rule = await save_ai_keyword(client)
    url = "/api/groups/-1001/rules/preview"
    async def preview(text):
        response = await client.post(url, json={"text": text})
        assert response.status_code == 200
        result = response.json()
        assert not result["delete_message"] and not result["mute_member"]
        return result["ai_confirmation_required"]
    assert await preview("纸飞机内容")
    rule = await save_ai_keyword(client, previous=rule, value="南瓜")
    assert not await preview("纸飞机内容")
    assert await preview("南瓜内容")
    await save_ai_keyword(client, previous=rule, enabled=False)
    assert not await preview("南瓜内容")


@pytest.mark.parametrize("scope,body,name,expected", [
    ("either", "柴郡喵，这是纸.飞.机内容", "普通成员", True),
    ("either", "柴郡喵，这条需要判断", "纸1飞A机", True),
    ("name", "柴郡喵，这是纸飞机内容", "普通成员", False),
    ("message", "柴郡喵，这条需要判断", "纸飞机", False),
])
async def test_ai_keywords_use_independent_configured_fields(env, client, scope, body, name, expected):
    chat, model = await configured(env)
    await save_ai_keyword(client, scope=scope)
    preview = (await client.post("/api/groups/-1001/rules/preview", json={"text": body, "sender_name": name})).json()
    assert preview["ai_confirmation_required"] is expected
    assert not preview["delete_message"] and not preview["mute_member"]
    model.result = ChatReply(should_reply=False, is_advertisement=True)
    incoming = message(211, body)
    incoming["from"].update(first_name=name, username="sample_user")
    await env[3].message(incoming)
    await chat.observe(incoming)
    assert await chat.run_one() and len(model.calls) == 1
    await drain(env[3])
    assert any(method == "delete_message" for method, _ in chat.tg.calls) is expected
    assert any(method == "restrict_chat_member" for method, _ in chat.tg.calls) is expected
    assert not any(method == "ban_chat_member" for method, _ in chat.tg.calls)


async def test_name_edit_cancels_pending_ai_keyword_penalty(env, client):
    chat, model = await configured(env)
    await save_ai_keyword(client, scope="either")
    model.result = ChatReply(should_reply=False, is_advertisement=True)
    incoming = message(212, "柴郡喵，这条需要判断")
    incoming["from"]["first_name"] = "纸飞机"
    await chat.observe(incoming)
    assert await chat.run_one()
    async with chat.db.transaction() as session:
        row = await session.scalar(select(ChatMessage).where(ChatMessage.message_id == 212))
        row.sender_name = "普通成员"
    await drain(env[3])
    assert not any(method in ("delete_message", "restrict_chat_member") for method, _ in chat.tg.calls)


@pytest.mark.parametrize("text,entities", [
    ("柴郡喵，纸飞机 https://example.test", []),
    ("柴郡喵，纸飞机 example.test", []),
    ("柴郡喵，纸.飞.机内容", [{"type": "url", "offset": 4, "length": 8}]),
    ("柴郡喵，纸.飞.机内容", [{"type": "text_link", "offset": 4, "length": 8, "url": "https://例子.测试"}]),
])
async def test_keyword_does_not_bypass_actual_link_exclusion(env, client, text, entities):
    chat, model = await configured(env)
    await save_ai_keyword(client, scope="either")
    await chat.observe(message(214, text, entities=entities))
    assert not await chat.run_one() and not model.calls


def test_dotted_word_ambiguity_uses_current_keyword_data():
    from purrai.chat_links import has_link

    rule = {"kind": "keyword", "value": "纸飞机", "scope": "either", "mode": "pinyin",
            "penalty": "ai_confirmed_mute", "enabled": True}
    text = "这是纸.飞.机内容"
    assert not has_link(text, rules=[rule])
    assert has_link(text, rules=[{**rule, "enabled": False}])
    assert has_link(text, rules=[{**rule, "value": "南瓜"}])
    assert has_link("主页.中国")


@pytest.mark.parametrize("verdict", [True, False])
async def test_retired_templates_and_combinations_leave_candidates_for_ai(env, client, verdict):
    chat, model = await configured(env)
    # Disable legacy combinations through their existing management API. Neither
    # the seed nor the matching engine needs a vocabulary change.
    original = (await client.get("/api/settings/business-policy")).json()
    document = original["document"]
    for stage in document["stages"]:
        stage["enabled"] = False
        for rule in stage["rules"]:
            rule["enabled"] = False
    saved = await client.put("/api/settings/business-policy", headers={"If-Match": original["version"]},
                            json={"document": document, "request_id": "retire-legacy-combinations"})
    assert saved.status_code == 200, saved.text
    old = await save_ai_keyword(client, value="招代收日入7000", mode="compact", penalty="permanent_mute")
    baseline = (await client.get("/api/settings/defaults")).json()
    saved = await client.patch("/api/settings/defaults", json={
        "rules": [{**old, "enabled": False}, {"kind": "keyword", "value": "代收", "mode": "pinyin",
            "scope": "either", "penalty": "ai_confirmed_mute"}],
        "baseline": baseline, "request_id": "replace-template-with-keyword"})
    assert saved.status_code == 200, saved.text
    model.result = ChatReply(should_reply=False, is_advertisement=verdict)
    incoming = message(213, "柴郡喵，招代收日入7000")
    await env[3].message(incoming)
    await drain(env[3])
    assert not any(method in ("delete_message", "restrict_chat_member", "ban_chat_member") for method, _ in chat.tg.calls)
    await chat.observe(incoming)
    assert await chat.run_one() and len(model.calls) == 1
    await drain(env[3])
    assert any(method == "delete_message" for method, _ in chat.tg.calls) is verdict
    assert any(method == "restrict_chat_member" for method, _ in chat.tg.calls) is verdict
    assert not any(method == "ban_chat_member" for method, _ in chat.tg.calls)


@pytest.mark.parametrize("change", ["disable", "replace", "penalty", "switch", "edit", "whitelist", "admin"])
async def test_queued_keyword_ai_actions_recheck_rules_message_and_exemptions(env, client, change):
    chat, model = await configured(env)
    rule = await save_ai_keyword(client)
    model.result = ChatReply(should_reply=False, is_advertisement=True)
    await chat.observe(message(203, "柴郡喵，纸飞机内容"))
    assert await chat.run_one()
    if change in ("disable", "replace", "penalty"):
        changes = {"disable": {"enabled": False}, "replace": {"value": "南瓜"}, "penalty": {"penalty": "delete"}}[change]
        await save_ai_keyword(client, previous=rule, **changes)
    elif change == "admin":
        chat.tg.members[(-1001, 42)] = {"status": "administrator"}
    else:
        async with chat.db.transaction() as session:
            if change == "switch":
                group = await session.get(Group, -1001)
                group.settings = {**group.settings, "chat_advertiser_cooldown": False}
            elif change == "whitelist":
                session.add(Whitelist(group_id=-1001, user_id=42, moderation=True))
            else:
                row = await session.scalar(select(ChatMessage).where(ChatMessage.message_id == 203))
                row.text = "纸飞机仍在，但原文已经修改，旧AI结果无效"
    await drain(env[3])
    assert not any(method in ("delete_message", "restrict_chat_member") for method, _ in chat.tg.calls)


async def test_keyword_change_during_role_lookup_cancels_mute_without_claiming_restriction(env, client):
    chat, model = await configured(env)
    await save_ai_keyword(client)
    # Use the per-group management endpoint as well as shared rule configuration.
    response = await client.post("/api/groups/-1001/rules", json={"kind": "keyword", "value": "南瓜",
        "mode": "contains", "scope": "message", "penalty": "ai_confirmed_mute", "enabled": True,
        "request_id": "create-conditional-rule"}, headers={"Idempotency-Key": "create-conditional-rule"})
    assert response.status_code == 200, response.text
    model.result = ChatReply(should_reply=False, is_advertisement=True)
    await chat.observe(message(204, "柴郡喵，南瓜内容"))
    assert await chat.run_one()
    original_call = chat.tg.call
    async def change_on_lookup(method, **kwargs):
        result = await original_call(method, **kwargs)
        if method == "get_chat":
            async with chat.db.transaction() as session:
                rule = await session.scalar(select(Rule).where(Rule.value == "南瓜"))
                rule.enabled = False
        return result
    chat.tg.call = change_on_lookup
    await drain(env[3])
    assert not any(method == "restrict_chat_member" for method, _ in chat.tg.calls)
    async with chat.db.sessions() as session:
        member = await session.get(Member, (-1001, 42))
        assert member.owned_restriction is None


@pytest.mark.parametrize("option", ["chat_enabled", "chat_advertiser_cooldown"])
async def test_ai_keyword_preserves_existing_switches(env, client, option):
    chat, model = await configured(env, **{option: False})
    await save_ai_keyword(client)
    model.result = ChatReply(should_reply=False, is_advertisement=True)
    await chat.observe(message(205, "柴郡喵，纸飞机内容"))
    await chat.run_one()
    await drain(env[3])
    assert not any(method in ("delete_message", "restrict_chat_member") for method, _ in chat.tg.calls)
    if option == "chat_enabled":
        assert not model.calls
