from types import SimpleNamespace

import pytest

from purrai.models import Group, SharedPolicy
from purrai.promotion_targets import advertised_targets, blacklist_reasons, normalize_target, remember_targets
from purrai.rules import assess
from purrai.schemas import GroupSettings, RuleInput
from purrai.shared_policy import effective_rules


@pytest.mark.parametrize("value,expected", [
    ("https://t.me/LongTerm/123", "account:longterm"),
    ("tg://resolve?domain=LongTerm&post=456", "account:longterm"),
    ("https://t.me/c/9001/123", "channel:-1009001"),
    ("https://t.me/+AbCdEf123456", "invite:AbCdEf123456"),
    ("+86 (138) 0013-8000", "phone:8613800138000"),
    ("hello@Example.com", "email:hello@example.com"),
    ("https://www.example.com/promotion/123", "domain:example.com"),
    ("https://wa.me/8613800138000", "phone:8613800138000"),
    ("https://t.me/+8613800138000", "phone:8613800138000"),
])
def test_target_identity_ignores_posts_and_normalizes_contacts(value, expected):
    assert normalize_target(value) == expected
    assert normalize_target(expected) == expected


def test_domain_whitelist_keeps_posts_allowed_but_still_checks_advertising_text():
    rules = [{"kind": "allow_domain", "value": "x.com", "enabled": True}]
    settings = GroupSettings(newcomer_wallets=False).model_dump()
    assert not assess("正常分享", ["https://x.com/anyone/status/123"], rules, settings, newcomer=True)["reasons"]
    assert assess("免费带单，私聊我领取策略", ["https://x.com/anyone/status/123"], rules, settings)["ban_reasons"]
    assert advertised_targets({"text": "https://x.com/anyone/status/123"}, rules) == []
    assert advertised_targets({"text": "https://x.com.evil.example/post"}, rules) == ["domain:x.com.evil.example"]
    youtube = [{"kind": "allow_domain", "value": "youtube.com", "enabled": True}]
    assert advertised_targets({"text": "https://youtube.com/@some_creator"}, youtube) == []


async def test_learned_destinations_apply_to_other_groups_without_learning_disposable_sender(env):
    _, db, _, _ = env
    message = {"message_id": 10, "chat": {"id": -1001}, "from": {"id": 42, "username": "burner"},
               "text": "免费带单，联系 @LongTerm @burner https://x.com/author/status/123",
               "forward_origin": {"type": "channel", "chat": {"id": -1009001, "type": "channel", "username": "channel_old"}},
               "contact": {"phone_number": "+86 138-0013-8000", "user_id": 99}}
    violation = SimpleNamespace(message_id=10, rule_mute=False, reasons=["明确广告引流：业务或收益招揽并要求联系"])
    async with db.transaction() as session:
        session.add(SharedPolicy(id=1, settings=GroupSettings().model_dump(),
                                 rules=[{"id": "allowx", "kind": "allow_domain", "value": "x.com", "enabled": True}]))
        await session.flush()
        group = await session.get(Group, -1001)
        learned = await remember_targets(session, group, violation, {"message": message})
        assert set(learned) == {"account:longterm", "account:channel_old", "channel:-1009001", "phone:8613800138000", "contact:99"}
        assert await remember_targets(session, group, violation, {"message": message}) == []
    async with db.sessions() as session:
        other_rules = await effective_rules(session, -1002)
    assert blacklist_reasons({"text": "联系 @LongTerm"}, other_rules)
    assert blacklist_reasons({"text": "", "forward_origin": {"type": "channel", "chat": {
        "id": -1009001, "type": "channel", "username": "renamed_channel"}}}, other_rules)
    assert blacklist_reasons({"text": "请联系 +86 (138) 0013-8000"}, other_rules)
    assert not blacklist_reasons({"text": "@burner"}, other_rules)


async def test_reports_and_non_advertising_violations_do_not_learn_targets(env):
    _, db, _, _ = env
    async with db.transaction() as session:
        group = await session.get(Group, -1001)
        violation = SimpleNamespace(message_id=11, rule_mute=False, reasons=["重复刷屏"])
        assert await remember_targets(session, group, violation, {"message": {"text": "@normal_user"}}) == []
        violation.reasons = ["明确广告引流：测试"]
        assert await remember_targets(session, group, violation, {"message": {
            "text": "举报：这个 @normal_user 是骗子，大家别进"}}) == []


def test_target_rule_is_editable_and_can_be_disabled():
    body = RuleInput(kind="block_target", value="https://t.me/longterm/123")
    from purrai.rule_admin import RuleAdministration
    RuleAdministration.validate(body)
    assert body.value == "account:longterm"
    assert not blacklist_reasons({"text": "@longterm"}, [{"kind": body.kind, "value": body.value, "enabled": False}])


def test_shared_shorteners_only_blacklist_the_specific_link_and_phone_contacts_are_normalized():
    targets = advertised_targets({"text": "电话：13800138000 https://t.co/one https://wa.me/8613800138000"})
    assert set(targets) == {"phone:13800138000", "phone:8613800138000", "url:https://t.co/one"}
    rules = [{"kind": "block_target", "value": "url:https://t.co/one", "enabled": True}]
    assert blacklist_reasons({"text": "https://t.co/one"}, rules)
    assert not blacklist_reasons({"text": "https://t.co/two"}, rules)


async def test_real_advertising_path_learns_a_target_for_another_group(env):
    from conftest import drain, message
    _, db, _, service = env
    await service.message(message(80, "免费带单，私聊我领取策略 @LongTerm"))
    await drain(service)
    async with db.sessions() as session:
        other_rules = await effective_rules(session, -1002)
    assert blacklist_reasons({"text": "@LongTerm"}, other_rules)
