import pytest
from sqlalchemy import select

from conftest import drain, message
from purrai.message_identity import message_sender
from purrai.models import Action, ArchivedMessage, Group, MessageRevision, Violation, Whitelist
from purrai.rules import assess, message_content
from purrai.schemas import GroupSettings

PROMO = "点下方链接，限时免费看禁忌资源\n点我搜索 酒店偷拍\n福利姬付费门槛免费看"


def guest(identifier, body=PROMO):
    return message(identifier, body,
        **{"from": {"id": 7000 + identifier, "is_bot": True, "first_name": "BrookSun"}},
        guest_bot_caller_user={"id": 42, "is_bot": False, "first_name": "Sai Sai", "username": "Sai200820"},
        entities=[{"type": "text_link", "offset": 0, "length": 5, "url": "https://t.me/resource_bot?start=abc"}])


async def local_rules_only(db):
    async with db.transaction() as session:
        group = await session.get(Group, -1001)
        group.settings = {**group.settings, "external_group_ban": False, "external_invite_ban": False}


@pytest.mark.parametrize("caption", [False, True])
async def test_resource_ad_deletes_and_permanently_bans_actual_guest_caller(env, caption):
    _, db, tg, svc = env
    await local_rules_only(db)
    incoming = guest(1)
    if caption:
        incoming.update(caption=incoming.pop("text"), caption_entities=incoming.pop("entities"), photo=[{"file_id": "p"}])
    await svc.message(incoming)
    await drain(svc)
    assert tg.members[(-1001, 42)] == {"status": "kicked", "until_date": 0}
    assert [kw["user_id"] for method, kw in tg.calls if method == "ban_chat_member"] == [42]
    assert [kw["message_id"] for method, kw in tg.calls if method == "delete_message"] == [1]
    async with db.sessions() as session:
        archive = await session.scalar(select(ArchivedMessage))
        original = await session.scalar(select(MessageRevision))
        violation = await session.scalar(select(Violation))
        assert archive.sender_id == violation.user_id == 42
        assert original.payload["from"]["id"] == 7001
        assert violation.sender_name == "Sai Sai @Sai200820"


async def test_changing_guest_bots_does_not_reset_caller_repeat_count(env):
    _, db, tg, svc = env
    await local_rules_only(db)
    async with db.transaction() as session:
        group = await session.get(Group, -1001)
        group.settings = {**group.settings, "repeat_count": 3, "similar_enabled": False, "burst_enabled": False}
    for identifier in (1, 2, 3):
        incoming = guest(identifier, "今天我们一起聊聊周末的安排。")
        incoming.pop("entities")
        await svc.message(incoming)
    await drain(svc)
    restrictions = [kw for method, kw in tg.calls if method == "restrict_chat_member"]
    assert len(restrictions) == 1 and restrictions[0]["user_id"] == 42 and restrictions[0]["until_date"] > 0


@pytest.mark.parametrize("exemption", ["administrator", "whitelist", "disabled"])
async def test_guest_caller_keeps_existing_moderation_protections(env, exemption):
    _, db, tg, svc = env
    await local_rules_only(db)
    async with db.transaction() as session:
        if exemption == "whitelist":
            session.add(Whitelist(group_id=-1001, user_id=42, moderation=True))
        if exemption == "disabled":
            group = await session.get(Group, -1001)
            group.enabled = False
    if exemption == "administrator":
        tg.members[(-1001, 42)] = {"status": "administrator"}
    await svc.message(guest(1))
    await drain(svc)
    assert not any(method in ("delete_message", "ban_chat_member", "restrict_chat_member") for method, _ in tg.calls)


@pytest.mark.parametrize("body", [
    "限时免费看公开课资源", "这里是酒店偷拍案的新闻报道", "如何举报偷拍广告？",
    "举报：有人发酒店偷拍，号称限时免费看，请管理员处理",
    "“点下方链接，限时免费看酒店偷拍” 这种广告怎么识别？",
])
def test_ordinary_resources_reports_and_quotes_are_not_promotions(body):
    incoming = guest(1, body)
    text, urls = message_content(incoming)
    assert not assess(text, urls, [], GroupSettings().model_dump())["ban_reasons"]


def test_human_message_cannot_override_sender_with_guest_field():
    incoming = guest(1)
    incoming["from"] = {"id": 43, "is_bot": False}
    assert message_sender(incoming)["id"] == 43


async def test_disabling_advertising_before_queued_ban_cancels_it(env):
    _, db, tg, svc = env
    await local_rules_only(db)
    await svc.message(guest(1))
    async with db.transaction() as session:
        group = await session.get(Group, -1001)
        group.settings = {**group.settings, "advertising_ban": False}
    await drain(svc)
    assert not any(method == "ban_chat_member" for method, _ in tg.calls)
    async with db.sessions() as session:
        assert (await session.scalar(select(Action).where(Action.kind == "advertisement_ban"))).state == "cancelled"
