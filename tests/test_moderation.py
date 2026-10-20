import time

from sqlalchemy import select

from conftest import drain, message
from purrai.models import Action, Member, Rule, Violation, Whitelist
from purrai.rules import domain_matches, evaluate, message_content
from purrai.models import default_settings


async def rule(database, kind="keyword", value="广告", **kwargs):
    async with database.transaction() as session:
        session.add(Rule(group_id=-1001, kind=kind, value=value, **kwargs))


async def test_text_edit_multi_match_only_deletes_once_without_warning(env):
    _, database, _, service = env
    await rule(database)
    await rule(database, value="告")
    await service.message(message(1, "正常消息"))
    await service.message(message(1, "广告"), edited=True)
    await service.message(message(1, "广告广告"), edited=True)
    async with database.sessions() as session:
        rows = (await session.scalars(select(Violation))).all()
        assert len(rows) == 1 and len(rows[0].reasons) == 2
        assert len((await session.scalars(select(Action).where(Action.kind == "delete"))).all()) == 1
        assert not rows[0].warning_active
        assert not (
            await session.scalars(
                select(Action).where(Action.kind.in_(("warn", "moderation_mute", "profile_mute")))
            )
        ).all()


async def test_multiple_body_violations_and_sender_channel_only_delete(env):
    _, database, telegram, service = env
    await rule(database)
    for identifier in range(3):
        await service.message(message(identifier, "广告"))
    await drain(service)
    assert not any(method in ("restrict_chat_member", "ban_chat_member") for method, _ in telegram.calls)
    assert len([1 for method, _ in telegram.calls if method == "delete_message"]) == 3
    await service.message(message(5, "广告", sender_chat={"id": -2002}))
    await drain(service)
    assert not any(
        method in ("restrict_chat_member", "ban_chat_member") and kwargs["user_id"] == -2002
        for method, kwargs in telegram.calls
    )


async def test_newcomer_contacts_and_hidden_link(env):
    _, database, _, service = env
    async with database.transaction() as session:
        session.add(Member(group_id=-1001, user_id=42, passed_at=time.time(), joined_at=time.time()))
    await service.message(
        message(
            1,
            "😀 点击",
            entities=[{"type": "text_link", "offset": 3, "length": 2, "url": "https://spam.example"}],
        )
    )
    await service.message(message(2, "", contact={"phone_number": "+12345"}))
    async with database.sessions() as session:
        assert len((await session.scalars(select(Violation))).all()) == 2


async def test_whitelist_and_admin_exempt(env):
    _, database, telegram, service = env
    await rule(database)
    async with database.transaction() as session:
        session.add(Whitelist(group_id=-1001, user_id=42, verification=True, moderation=True))
    telegram.members[(-1001, 43)] = {"status": "administrator"}
    await service.message(message(1, "广告"))
    await service.message(message(2, "广告", user_id=43))
    async with database.sessions() as session:
        assert not (await session.scalars(select(Violation))).all()


async def test_repeat_rule_does_not_count_edited_message_twice(env):
    _, database, _, service = env
    for identifier in range(4):
        await service.message(message(identifier, "重复内容"))
    await service.message(message(3, "重复内容"), edited=True)
    async with database.sessions() as session:
        assert not (await session.scalars(select(Violation))).all()
    await service.message(message(4, "重复内容"))
    async with database.sessions() as session:
        assert len((await session.scalars(select(Violation))).all()) == 1


def test_allow_domain_only_exempts_link_not_keyword():
    rules = [{"kind": "allow_domain", "value": "example.com"}, {"kind": "keyword", "value": "推广"}]
    assert evaluate("推广", ["https://example.com"], rules, default_settings(), newcomer=True) == [
        "关键词：推广"
    ]
    assert domain_matches("sub.example.com", "example.com")
    assert not domain_matches("example.com.evil.test", "example.com")


def test_utf16_entity_offsets_and_captions():
    text, urls = message_content(
        {"caption": "😀 example.com", "caption_entities": [{"type": "url", "offset": 3, "length": 11}]}
    )
    assert urls == ["example.com"]
    assert text.startswith("😀")


def test_normalization_and_invites():
    assert evaluate("推\u200b广", [], [{"kind": "keyword", "value": "推广"}], default_settings())
    assert "群邀请链接" in evaluate("", ["t.me/+abc"], [], {**default_settings(), "block_invites": True})
