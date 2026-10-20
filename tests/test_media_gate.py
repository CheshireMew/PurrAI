import time

import pytest
from sqlalchemy import select

from conftest import drain, join, message
from purrai.models import Action, ArchivedMessage, Group, Inbox, MemberText, Rule, Violation, Whitelist
from purrai.runtime import Runtime
from purrai.service import Service


def photo(identifier, caption="", user_id=42):
    payload = message(identifier, "", user_id=user_id)
    payload.pop("text")
    payload.update(photo=[{"file_id": "photo", "file_unique_id": "p", "width": 100, "height": 100}])
    if caption:
        payload["caption"] = caption
    return payload


async def speak(service, user_id=42, start=1):
    for identifier, text in enumerate(("大家好", "今天天气不错", "想了解一下这个话题"), start):
        await service.message(message(identifier, text, user_id=user_id))


def deleted(telegram):
    return [args["message_id"] for method, args in telegram.calls if method == "delete_message"]


def assert_only_deletion(telegram):
    assert not any(
        method in ("restrict_chat_member", "ban_chat_member", "send_message", "send_photo")
        for method, _ in telegram.calls
    )


@pytest.mark.parametrize(
    "media",
    [
        {"photo": [{"file_id": "p"}]},
        {"sticker": {"file_id": "s"}},
        {"animation": {"file_id": "a"}},
        {"document": {"file_id": "d", "mime_type": "image/png"}},
        {"text": "😂👍"},
        {"text": "1️⃣"},
        {
            "text": "cat",
            "entities": [{"type": "custom_emoji", "offset": 0, "length": 3, "custom_emoji_id": "1"}],
        },
    ],
)
async def test_unqualified_media_is_deleted_without_member_penalty(env, media):
    _, database, telegram, service = env
    incoming = message(20, "")
    incoming.update(media)
    if "text" not in media:
        incoming.pop("text")
    await service.message(incoming)
    await service.message(incoming)
    await drain(service)
    assert deleted(telegram) == [20]
    assert_only_deletion(telegram)
    async with database.sessions() as session:
        violation = (await session.scalars(select(Violation))).one()
        assert not violation.warning_active and not violation.profile_violation
        assert [row.kind for row in await session.scalars(select(Action))] == ["delete"]


async def test_third_distinct_text_unlocks_media_and_caption_does_not_count(env):
    _, database, telegram, service = env
    await service.message(message(1, "第一句"))
    await service.message(photo(2, "第二句图片说明"))
    await service.message(message(3, "第二句"))
    await service.message(photo(4))
    await service.message(message(5, "第三句"))
    await service.message(photo(6, "这条图片可以发送"))
    await drain(service)
    assert deleted(telegram) == [2, 4]
    assert_only_deletion(telegram)
    async with database.sessions() as session:
        assert len((await session.scalars(select(MemberText))).all()) == 3


async def test_same_text_punctuation_edits_commands_and_emoji_do_not_inflate_credit(env):
    _, database, telegram, service = env
    await service.message(message(1, "你好"))
    await service.message(message(2, "你 好！！"))
    await service.message(message(1, "编辑的第二句"), edited=True)
    await service.message(message(3, "/help"))
    await service.message(message(4, "😂"))
    await service.message(message(5, "???"))
    await service.message(photo(6))
    await drain(service)
    assert deleted(telegram) == [4, 6]
    async with database.sessions() as session:
        assert len((await session.scalars(select(MemberText))).all()) == 1


async def test_text_credit_survives_restart_and_rejoin_but_is_group_local(env):
    settings, database, telegram, service = env
    await speak(service)
    restarted = Service(database, telegram, settings)
    await restarted.member_event(join(old="member", new="left"))
    async with database.transaction() as session:
        group = await session.get(Group, -1001)
        group.settings = {**group.settings, "verification_enabled": False}
        session.add(Group(approved=True, id=-1002, title="其他群", settings=group.settings, enabled=True))
    await restarted.member_event(join(date=time.time() + 1))
    await restarted.message(photo(20))
    incoming = photo(20)
    incoming["chat"] = {"id": -1002, "type": "supergroup"}
    await restarted.message(incoming)
    await drain(restarted)
    assert [
        (args["chat_id"], args["message_id"]) for method, args in telegram.calls if method == "delete_message"
    ] == [(-1002, 20)]


async def test_archive_disabled_still_tracks_text_and_caption_deleted_whole(env):
    _, database, telegram, service = env
    async with database.transaction() as session:
        group = await session.get(Group, -1001)
        group.settings = {**group.settings, "archive_enabled": False}
    await service.message(photo(1, "说明文字一起删"))
    await speak(service, start=2)
    await service.message(photo(10))
    await drain(service)
    assert deleted(telegram) == [1]
    assert_only_deletion(telegram)
    async with database.sessions() as session:
        assert not (await session.scalars(select(ArchivedMessage))).all()
        assert len((await session.scalars(select(MemberText))).all()) == 3


async def test_profile_violation_overrides_delete_only_media_gate(env):
    _, database, telegram, service = env
    async with database.transaction() as session:
        session.add(Rule(group_id=-1001, kind="keyword", value="广告", scope="name"))
        session.add(Rule(group_id=-1001, kind="keyword", value="广告", scope="message"))
    incoming = photo(1, "广告说明")
    incoming["from"]["first_name"] = "广告昵称"
    await service.message(incoming)
    await drain(service)
    assert deleted(telegram) == [1]
    assert telegram.members[(-1001, 42)]["until_date"] == 0
    assert not telegram.members[(-1001, 42)]["can_send_messages"]
    async with database.sessions() as session:
        archived = (await session.scalars(select(ArchivedMessage))).one()
        assert archived.caption == "广告说明" and archived.deletion_state == "deleted"


async def test_admin_whitelist_and_disabled_group_are_exempt(env):
    _, database, telegram, service = env
    telegram.members[(-1001, 43)] = {"status": "administrator"}
    async with database.transaction() as session:
        session.add(Whitelist(group_id=-1001, user_id=44, moderation=True))
    for identifier, user_id in enumerate((1, 43, 44), 1):
        await service.message(photo(identifier, user_id=user_id))
    async with database.transaction() as session:
        (await session.get(Group, -1001)).enabled = False
    await service.message(photo(10))
    await drain(service)
    assert not deleted(telegram)


async def test_media_threshold_can_be_changed_or_disabled(env):
    _, database, telegram, service = env
    async with database.transaction() as session:
        group = await session.get(Group, -1001)
        group.settings = {**group.settings, "media_min_text_messages": 1}
    await service.message(photo(1))
    await service.message(message(2, "第一句"))
    await service.message(photo(3))
    async with database.transaction() as session:
        group = await session.get(Group, -1001)
        group.settings = {**group.settings, "media_min_text_messages": 0}
    await service.message(photo(4, user_id=45))
    await drain(service)
    assert deleted(telegram) == [1]


async def test_bad_text_and_long_single_message_do_not_make_three_contributions(env):
    _, database, telegram, service = env
    async with database.transaction() as session:
        session.add(Rule(group_id=-1001, kind="keyword", value="广告"))
    await service.message(message(1, "广告"))
    await service.message(message(2, "第一句。第二句。第三句。"))
    await service.message(photo(3))
    await drain(service)
    assert deleted(telegram) == [1, 3]
    async with database.sessions() as session:
        assert len((await session.scalars(select(MemberText))).all()) == 1


async def test_history_counts_only_processed_originals_before_media(env):
    _, database, telegram, service = env
    runtime = Runtime(database, telegram, service)
    updates = []
    for identifier, text in enumerate(("旧第一句", "旧第二句", "旧第三句"), 1):
        incoming = message(identifier, text)
        incoming["from"].update(is_bot=False, first_name="旧群友")
        updates.append({"update_id": identifier, "message": incoming})
    await runtime.persist(updates)
    async with database.transaction() as session:
        for identifier in (1, 2, 3):
            (await session.get(Inbox, identifier)).state = "done"
    await service.message(photo(10))
    await drain(service)
    assert not deleted(telegram)
    async with database.sessions() as session:
        assert len((await session.scalars(select(MemberText))).all()) == 3


async def test_future_archived_or_unprocessed_text_does_not_unlock_earlier_media(env):
    _, database, telegram, service = env
    runtime = Runtime(database, telegram, service)
    updates = []
    for identifier in (1, 2, 11, 12, 13):
        incoming = message(identifier, f"尚未完成处理的消息{identifier}")
        incoming["from"].update(is_bot=False, first_name="测试")
        updates.append({"update_id": identifier, "message": incoming})
    await runtime.persist(updates)
    async with database.transaction() as session:
        for identifier in (11, 12, 13):
            (await session.get(Inbox, identifier)).state = "done"
    await service.message(photo(10))
    await drain(service)
    assert deleted(telegram) == [10]


async def test_history_with_violations_and_captions_does_not_grant_credit(env):
    _, database, telegram, service = env
    runtime = Runtime(database, telegram, service)
    updates = []
    for identifier in (1, 2, 3):
        incoming = message(identifier, f"历史广告{identifier}")
        incoming["from"].update(is_bot=False, first_name="测试")
        updates.append({"update_id": identifier, "message": incoming})
    incoming = photo(4, "历史图片说明")
    incoming["from"].update(is_bot=False, first_name="测试")
    updates.append({"update_id": 4, "message": incoming})
    await runtime.persist(updates)
    async with database.transaction() as session:
        for identifier in (1, 2, 3, 4):
            (await session.get(Inbox, identifier)).state = "done"
        for identifier in (1, 2, 3):
            session.add(
                Violation(
                    group_id=-1001,
                    user_id=42,
                    message_id=identifier,
                    reasons=["历史广告"],
                    warning_active=False,
                )
            )
    await service.message(photo(10))
    await drain(service)
    assert deleted(telegram) == [10]
    async with database.sessions() as session:
        assert not (await session.scalars(select(MemberText))).all()


async def test_setting_via_bot_changes_actual_media_gate(env):
    from test_bot_admin import click, deliver, open_group, private

    _, database, telegram, service = env
    runtime = Runtime(database, telegram, service)
    await open_group(runtime, telegram)
    await click(runtime, telegram, "防广告设置")
    await click(runtime, telegram, "发图及表情前独立文字条数")
    await deliver(runtime, {"message": private("1")})
    async with database.sessions() as session:
        assert (await session.get(Group, -1001)).settings["media_min_text_messages"] == 1
    await service.message(message(1000, "一句有效文字"))
    await service.message(photo(1001))
    await drain(service)
    assert not deleted(telegram)
