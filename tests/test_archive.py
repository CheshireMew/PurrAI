import json
import time

import pytest
from sqlalchemy import func, select

from conftest import drain, message
from purrai.archive import detail, export_jsonl, page
from purrai.management import Management, ManagementError
from purrai.models import Action, ArchivedMessage, Group, Inbox, MessageRevision, Rule
from purrai.runtime import Runtime
from purrai.schemas import ArchiveFilter
from test_bot_admin import click, deliver, markup, open_group, private


async def test_archive_before_moderation_failure_and_inbox_ack(env):
    _, db, tg, service = env
    runtime = Runtime(db, tg, service)
    payload = message(71, "必须先保存", first_name="unused")
    payload["from"].update(is_bot=False, first_name="小明")
    async with db.transaction() as session:
        session.add(Rule(group_id=-1001, kind="keyword", value="必须先保存"))
    await runtime.persist([{"update_id": 10, "message": payload}])
    tg.fail["get_chat_member"] = 1
    assert await runtime.process_one()
    async with db.sessions() as session:
        inbox = await session.get(Inbox, 10)
        assert inbox.state == "pending" and inbox.attempts == 1
        archived = (await session.scalars(select(ArchivedMessage))).one()
        assert archived.text == "必须先保存" and archived.revision_count == 1
    async with db.transaction() as session:
        (await session.get(Inbox, 10)).available_at = 0
    assert await runtime.process_one()
    async with db.sessions() as session:
        assert (await session.get(Inbox, 10)).payload == {"update_id": 10}
        assert await session.scalar(select(func.count()).select_from(MessageRevision)) == 1


async def test_all_group_senders_and_paused_protection_are_archived_but_private_is_not(env):
    _, db, tg, service = env
    async with db.transaction() as session:
        (await session.get(Group, -1001)).enabled = False
    for identifier, sender in [(1, 1), (2, 42)]:
        await service.message(message(identifier, "正常聊天", sender))
    await service.message(
        message(3, "频道发言", sender_chat={"id": -2000, "title": "频道", "username": "channel"})
    )
    await service.message(message(4, "匿名管理", sender_chat={"id": -1001, "title": "测试群"}))
    await service.message(private("私聊不保存"))
    result = await page(db, -1001, ArchiveFilter())
    assert result["total"] == 4
    assert {r["sender_type"] for r in result["items"]} == {"user", "chat"}
    assert not tg.calls


async def test_revisions_are_idempotent_searchable_and_keep_latest_after_old_replay(env):
    _, db, _, service = env
    original = message(10, "正常聊天")
    changed = {**original, "text": "后来加广告", "edit_date": original["date"] + 1}
    latest = {**changed, "text": "后来又改正常", "edit_date": original["date"] + 2}
    for payload, edited, identifier in [
        (original, False, 1),
        (changed, True, 2),
        (latest, True, 3),
        (latest, True, 3),
        (original, False, 1),
        (changed, True, 2),
    ]:
        await service.message(payload, edited, identifier)
    result = await page(db, -1001, ArchiveFilter(query="加广告"))
    assert result["total"] == 1
    row = await detail(db, -1001, result["items"][0]["id"])
    assert row["text"] == "后来又改正常" and row["revision_count"] == 3
    assert [r["payload"]["text"] for r in row["revisions"]] == ["正常聊天", "后来加广告", "后来又改正常"]
    assert row["original_observed"]


async def test_first_seen_edit_and_same_second_edits(env):
    _, db, _, service = env
    first = message(11, "编辑 A", edit_date=int(time.time()) + 1)
    await service.message(first, True, 100)
    await service.message({**first, "text": "编辑 B"}, True, 101)
    await service.message(first, True, 102)
    row = (await page(db, -1001, ArchiveFilter()))["items"][0]
    assert not row["original_observed"] and row["revision_count"] == 3 and row["text"] == "编辑 A"


async def test_advertisement_is_saved_in_full_before_delete_and_export_has_metadata(env, client):
    _, db, tg, service = env
    async with db.transaction() as session:
        session.add(Rule(group_id=-1001, kind="keyword", value="推广", mode="contains"))
    text = "推广" + "完整原文" * 300
    payload = message(
        20,
        text,
        entities=[{"type": "text_link", "offset": 0, "length": 2, "url": "https://hidden.example/path"}],
        reply_to_message={"message_id": 2, "text": "不能假装收到的旧内容"},
        message_thread_id=9,
    )
    await service.message(payload)
    response = await client.get("/api/groups/-1001/messages?flagged=true")
    assert response.status_code == 200
    row = response.json()["items"][0]
    assert row["text"] == text and row["deletion_state"] == "pending"
    await drain(service)
    response = await client.get(f"/api/groups/-1001/messages/{row['id']}")
    data = response.json()
    assert data["deletion_state"] == "deleted" and data["deleted_at"] > 0
    assert data["revisions"][0]["reasons"] == ["关键词：推广"]
    assert data["revisions"][0]["payload"]["reply_to_message"] == {"message_id": 2}
    response = await client.get("/api/groups/-1001/messages/export?flagged=true")
    assert response.status_code == 200 and "attachment" in response.headers["content-disposition"]
    exported = json.loads(response.text)
    assert exported["text"] == text and exported["schema_version"] == 1
    assert exported["revisions"][0]["payload"]["entities"][0]["url"] == "https://hidden.example/path"
    assert exported["actions"][0]["state"] == "succeeded"
    assert not any(method == "get_file" for method, _ in tg.calls)


async def test_media_caption_and_file_metadata_survive_delete_without_download(env):
    _, db, _, service = env
    async with db.transaction() as session:
        session.add(Rule(group_id=-1001, kind="keyword", value="推广", mode="contains"))
    media = message(21, "")
    media.pop("text")
    media.update(
        caption="推广图片",
        photo=[{"file_id": "photo-id", "file_unique_id": "unique", "width": 800, "height": 600}],
        caption_entities=[{"type": "text_link", "offset": 0, "length": 2, "url": "https://target.example"}],
        media_group_id="album-1",
    )
    await service.message(media)
    await drain(service)
    lines = [json.loads(line) async for line in export_jsonl(db, -1001, ArchiveFilter(message_type="photo"))]
    assert len(lines) == 1 and lines[0]["caption"] == "推广图片"
    raw = lines[0]["revisions"][0]["payload"]
    assert raw["photo"][0]["file_id"] == "photo-id" and raw["media_group_id"] == "album-1"


async def test_archive_toggle_preserves_history_and_unapproved_group_is_not_archived(env):
    settings, db, tg, service = env
    management = Management(db, tg, service, settings)
    await service.message(message(1, "记录这条"))
    await management.setting(1, -1001, "archive_enabled", False, "off")
    await service.message(message(2, "关闭后不记录"))
    assert (await page(db, -1001, ArchiveFilter()))["total"] == 1
    await management.setting(1, -1001, "archive_enabled", True, "on")
    await service.message(message(3, "重新记录"))
    await service.message(message(1, "新群第一条", chat={"id": -1002, "type": "supergroup", "title": "新群"}))
    assert (await page(db, -1001, ArchiveFilter()))["total"] == 2
    assert (await page(db, -1002, ArchiveFilter()))["total"] == 0


async def test_delete_retry_failure_and_already_absent_are_not_faked_success(env):
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import DeleteMessage

    _, db, tg, service = env
    async with db.transaction() as session:
        session.add(Rule(group_id=-1001, kind="keyword", value="推广", mode="contains"))
    await service.message(message(30, "推广"))
    tg.fail["delete_message"] = 1
    await service.run_one_action()
    row = (await page(db, -1001, ArchiveFilter()))["items"][0]
    assert row["deletion_state"] == "pending" and row["deletion_error"] and not row["deleted_at"]
    original_call = tg.call

    async def missing(method, **kwargs):
        if method == "delete_message":
            raise TelegramBadRequest(
                DeleteMessage(chat_id=-1001, message_id=30), "message to delete not found"
            )
        return await original_call(method, **kwargs)

    tg.call = missing
    async with db.transaction() as session:
        task = await session.get(Action, row["deletion_action_id"])
        task.available_at = 0
    await drain(service)
    row = (await page(db, -1001, ArchiveFilter()))["items"][0]
    assert row["deletion_state"] == "already_absent" and row["deleted_at"] > 0


async def test_archive_http_filters_pagination_and_auth(env, client):
    _, db, tg, service = env
    await service.message(message(40, "100%_完整匹配", user_id=42))
    await service.message(message(41, "100xx不匹配", user_id=43))
    result = await client.get(
        "/api/groups/-1001/messages", params={"query": "%_", "sender_id": 42, "size": 1}
    )
    assert result.status_code == 200 and result.json()["total"] == 1
    assert result.json()["items"][0]["message_id"] == 40
    assert (await client.get("/api/groups/-1001/messages?after=20&before=10")).status_code == 422
    assert (await client.get("/api/groups/-1001/messages?size=101")).status_code == 422
    assert (await client.get("/api/groups/-1001/messages/missing")).status_code == 404
    settings = env[0]
    management = Management(db, tg, service, settings)
    with pytest.raises(ManagementError):
        await management.archived_messages(7, -1001, ArchiveFilter())
    client.cookies.clear()
    for suffix in ("", "/export", "/missing"):
        assert (await client.get("/api/groups/-1001/messages" + suffix)).status_code == 401


async def test_bot_archive_search_detail_export_and_toggle(env):
    _, db, tg, service = env
    runtime = Runtime(db, tg, service)
    await service.message(message(50, "分析样本"))
    await open_group(runtime, tg)
    await click(runtime, tg, "消息归档与导出")
    assert "共 1 条" in markup(tg)["text"]
    await click(runtime, tg, "按关键词搜索")
    await deliver(runtime, {"message": private("分析样本")})
    assert "分析样本" in markup(tg)["text"]
    await click(runtime, tg, "· 42 ·")
    assert "分析样本" in markup(tg)["text"]
    await click(runtime, tg, "返回归档列表")
    await click(runtime, tg, "导出当前筛选")
    await drain(service)
    sent = [kwargs for method, kwargs in tg.calls if method == "send_document"]
    assert len(sent) == 1 and sent[0]["chat_id"] == 1
    assert json.loads(sent[0]["document"])["text"] == "分析样本"
    await click(runtime, tg, "关闭消息记录")
    async with db.sessions() as session:
        assert not (await session.get(Group, -1001)).settings["archive_enabled"]


async def test_export_rechecks_admin_identity_before_sending(env):
    settings, db, tg, service = env
    tg.members[(-1001, 7)] = {"status": "administrator"}
    management = Management(db, tg, service, settings)
    await service.message(message(1, "不可外泄"))
    task = await management.archive_export(7, -1001, ArchiveFilter(), "export-task")
    tg.members[(-1001, 7)] = {"status": "member"}
    await drain(service)
    async with db.sessions() as session:
        current = await session.get(Action, task["id"])
        assert current.state == "cancelled" and "操作者当前已无此群管理权限" in current.error
    assert not any(method == "send_document" for method, _ in tg.calls)


async def test_delete_permission_failure_keeps_content_and_reports_failure(env):
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import DeleteMessage

    _, db, tg, service = env
    async with db.transaction() as session:
        session.add(Rule(group_id=-1001, kind="keyword", value="推广", mode="contains"))
    await service.message(message(90, "推广原文不能丢"))
    original = tg.call

    async def forbidden(method, **kwargs):
        if method == "delete_message":
            raise TelegramBadRequest(DeleteMessage(chat_id=-1001, message_id=90), "not enough rights")
        return await original(method, **kwargs)

    tg.call = forbidden
    await drain(service)
    row = (await page(db, -1001, ArchiveFilter(deletion="failed")))["items"][0]
    assert row["text"] == "推广原文不能丢" and "not enough rights" in row["deletion_error"]
    assert not row["deleted_at"]


async def test_stream_export_crosses_batch_boundary_without_omitting_or_leaking_groups(env, client):
    _, db, tg, service = env
    runtime = Runtime(db, tg, service)
    await runtime.persist(
        [{"update_id": 1000 + i, "message": message(i, f"群消息 {i}")} for i in range(1, 106)]
    )
    await runtime.persist(
        [
            {
                "update_id": 2000,
                "message": message(
                    1, "另一个群", chat={"id": -1002, "type": "supergroup", "title": "其他群"}
                ),
            }
        ]
    )
    response = await client.get("/api/groups/-1001/messages/export")
    data = [json.loads(line) for line in response.text.splitlines()]
    assert [row["message_id"] for row in data] == list(range(1, 106))
    assert all(row["group_id"] == -1001 and len(row["revisions"]) == 1 for row in data)
    result = await client.get("/api/groups/-1001/messages?page_number=2&size=100")
    assert result.json()["total"] == 105 and len(result.json()["items"]) == 5
    identifier = data[0]["id"]
    assert (await client.get(f"/api/groups/-1002/messages/{identifier}")).status_code == 404


async def test_disabled_receipts_are_not_backfilled_if_enabled_before_processing(env):
    settings, db, tg, service = env
    runtime = Runtime(db, tg, service)
    management = Management(db, tg, service, settings)
    await management.setting(1, -1001, "archive_enabled", False, "off-at-receipt")
    payload = message(200, "关闭期间收到，不应补录")
    payload["from"].update(is_bot=False, first_name="小明")
    await runtime.persist([{"update_id": 3000, "message": payload}])
    await management.setting(1, -1001, "archive_enabled", True, "enable-before-processing")
    await runtime.process_one()
    assert (await page(db, -1001, ArchiveFilter()))["total"] == 0
    await runtime.persist(
        [{"update_id": 3001, "message": {**payload, "message_id": 201, "text": "重新开启后收到"}}]
    )
    await runtime.process_one()
    result = await page(db, -1001, ArchiveFilter())
    assert result["total"] == 1 and result["items"][0]["message_id"] == 201
