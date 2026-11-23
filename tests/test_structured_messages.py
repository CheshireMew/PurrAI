"""Same editable rules and exact-revision enforcement across Telegram content formats."""
import pytest
from aiogram.types import Message, Update
from sqlalchemy import select

from conftest import drain, message
from purrai.advertisement_store import revision
from purrai.message_buttons import has_inline_buttons, inline_buttons
from purrai.message_emoji import identifiers
from purrai.message_parts import message_entities, message_text
from purrai.models import AdvertisementReview, ArchivedMessage, Group, MessageRevision
from purrai.telegram import telegram_data
from purrai.runtime import Runtime
from purrai.text_matching import message_content

FILE = {"file_id": "synthetic", "file_unique_id": "synthetic"}
PHOTO = {**FILE, "width": 1, "height": 1}


def forms(value):
    return {
        "checklist_title": {"checklist": {"title": value, "tasks": [{"id": 1, "text": "事项"}]}},
        "checklist_task": {"checklist": {"title": "待办", "tasks": [{"id": 1, "text": value}]}},
        "poll_question": {"poll": poll(value)},
        "poll_option": {"poll": poll("问题", value)},
        "poll_explanation": {"poll": {**poll("问题"), "explanation": value}},
        "poll_description": {"poll": {**poll("问题"), "description": value}},
        "venue_title": {"venue": {"title": value, "address": "地址", "location": {"latitude": 0, "longitude": 0}}},
        "venue_address": {"venue": {"title": "地点", "address": value, "location": {"latitude": 0, "longitude": 0}}},
        "document_name": {"document": {**FILE, "file_name": value}},
        "animation_name": {"animation": {**PHOTO, "duration": 1, "file_name": value}},
        "video_name": {"video": {**PHOTO, "duration": 1, "file_name": value}},
        "audio_title": {"audio": {**FILE, "duration": 1, "title": value}},
        "audio_performer": {"audio": {**FILE, "duration": 1, "performer": value}},
        "audio_name": {"audio": {**FILE, "duration": 1, "file_name": value}},
        "game_title": {"game": {"title": value, "description": "说明", "photo": [PHOTO]}},
        "game_description": {"game": {"title": "游戏", "description": value, "photo": [PHOTO]}},
        "game_text": {"game": {"title": "游戏", "description": "说明", "photo": [PHOTO], "text": value}},
        "invoice_title": {"invoice": {"title": value, "description": "说明", "start_parameter": "probe", "currency": "XTR", "total_amount": 1}},
        "invoice_description": {"invoice": {"title": "账单", "description": value, "start_parameter": "probe", "currency": "XTR", "total_amount": 1}},
        "rich_paragraph": {"rich_message": {"blocks": [{"type": "paragraph", "text": [value[:2], {"type": "bold", "text": value[2:]}]}]}},
        "rich_table": {"rich_message": {"blocks": [{"type": "table", "cells": [[{"text": value, "align": "left", "valign": "top"}]]}]}},
        "rich_list": {"rich_message": {"blocks": [{"type": "list", "items": [{"label": value, "blocks": [{"type": "paragraph", "text": "事项"}]}]}]}},
        "rich_summary": {"rich_message": {"blocks": [{"type": "details", "summary": {"type": "bold", "text": value}, "blocks": []}]}},
        "rich_caption": {"rich_message": {"blocks": [{"type": "photo", "photo": [PHOTO], "caption": {"text": value}}]}},
        "button_label": {"reply_markup": {"inline_keyboard": [[{"text": value, "callback_data": "probe"}]]}},
        "button_copy": {"reply_markup": {"inline_keyboard": [[{"text": "复制", "copy_text": {"text": value}}]]}},
        "rich_button": {"rich_message": {"blocks": [{"type": "buttons", "buttons": [{"text": value, "callback_data": "probe"}]}]}},
    }


def poll(question, option="选项"):
    return {"id": "probe", "question": question, "options": [{"persistent_id": "option", "text": option, "voter_count": 0}],
            "total_voter_count": 0, "is_closed": False, "is_anonymous": True, "type": "regular", "allows_multiple_answers": False, "allows_revoting": False, "members_only": False}


def incoming(fields, identifier=801):
    payload = message(identifier, "", **{"from": {"id": 42, "is_bot": False, "first_name": "群友"}})
    payload.pop("text")
    payload.update(fields)
    return telegram_data(Message.model_validate(payload))


@pytest.mark.parametrize("kind", forms("任意可替换内容"))
def test_visible_fields_reach_text_and_urls_after_real_sdk_serialization(kind):
    term = "任意可替换内容"
    msg = incoming(forms(term)[kind])
    assert term in message_text(msg)
    assert term in message_content(msg)[0]
    link = "https://t.me/+SyntheticContent123"
    msg = incoming(forms(link)[kind])
    assert link in message_content(msg)[1]
    changed = incoming(forms("已替换的新内容")[kind])
    assert revision(changed) != revision(msg)


@pytest.mark.parametrize("section", ["title", "task", "poll_question", "poll_option", "game", "rich"])
def test_entities_use_each_segment_utf16_offsets_and_preserve_custom_emoji(section):
    url = "https://t.me/+SyntheticEntity123"
    body = "😀 " + url
    entities = [{"type": "url", "offset": 3, "length": len(url)},
                {"type": "custom_emoji", "offset": 0, "length": 2, "custom_emoji_id": "1234567"},
                {"type": "text_mention", "offset": 0, "length": 2, "user": {"id": 77, "is_bot": False, "first_name": "指向账号"}}]
    if section == "title":
        fields = {"checklist": {"title": body, "title_entities": entities, "tasks": [{"id": 1, "text": "事项"}]}}
    elif section == "task":
        fields = {"checklist": {"title": "偏移量独立", "tasks": [{"id": 1, "text": body, "text_entities": entities}]}}
    elif section == "poll_question":
        fields = {"poll": {**poll(body), "question_entities": entities}}
    elif section == "poll_option":
        p = poll("另一个偏移量", body)
        p["options"][0]["text_entities"] = entities
        fields = {"poll": p}
    elif section == "game":
        fields = {"game": {"title": "游戏", "description": "说明", "photo": [PHOTO], "text": body, "text_entities": entities}}
    else:
        fields = {"rich_message": {"blocks": [{"type": "paragraph", "text": [
            {"type": "custom_emoji", "custom_emoji_id": "1234567", "alternative_text": "😀"},
            {"type": "url", "text": "隐藏入口", "url": url},
            {"type": "text_mention", "text": "账号", "user": entities[2]["user"]}]}]}}
    msg = incoming(fields)
    assert message_content(msg)[1] == [url]
    assert identifiers(msg) == ["1234567"]
    assert any(item.get("user", {}).get("id") == 77 for item in message_entities(msg))


def test_replies_quotes_forward_metadata_and_nested_snapshots_are_not_own_content():
    foreign = incoming(forms("不得归给回复者 https://t.me/+SyntheticForeign123")["checklist_task"])
    foreign["checklist"]["tasks"][0]["text_entities"] = [{"type": "custom_emoji", "offset": 0, "length": 1, "custom_emoji_id": "999999"}]
    msg = {"text": "我在回复", "reply_to_message": foreign, "pinned_message": foreign,
        "external_reply": {"chat": {"id": -2002}, "checklist": foreign["checklist"]},
        "quote": {"text": "不得归给回复者", "entities": foreign["checklist"]["tasks"][0]["text_entities"]},
        "forward_origin": {"type": "hidden_user", "sender_user_name": "不得归给回复者"}}
    assert message_content(msg) == ("我在回复", [])
    assert identifiers(msg) == []
    assert not has_inline_buttons(msg)


@pytest.mark.parametrize("kind", ["checklist_task", "poll_option", "venue_address", "document_name", "audio_title", "game_description", "invoice_description", "rich_paragraph", "rich_button"])
async def test_editable_rule_lifecycle_and_exact_revisions_for_structured_content(client, env, kind):
    _, db, tg, service = env
    first, replacement = "琥珀星际测试词", "青蓝山川替换词"
    rule = {"kind": "keyword", "value": first, "mode": "contains", "penalty": "delete"}
    created = await client.post("/api/groups/-1001/rules", json=rule, headers={"Idempotency-Key": "structured-rule-create"})
    assert created.status_code == 200, created.text
    row = created.json()
    msg = incoming(forms(first)[kind])
    await service.message(msg, update_id=8001)
    # Editing before queued side effects must withdraw the old decision.
    clean = incoming(forms("普通内容")[kind])
    clean["edit_date"] = msg["date"] + 1
    await service.message(clean, edited=True, update_id=8002)
    await drain(service)
    assert not any(method == "delete_message" for method, _ in tg.calls)
    msg = incoming(forms(first)[kind], 802)
    await service.message(msg, update_id=8003)
    await drain(service)
    assert any(method == "delete_message" and data["message_id"] == 802 for method, data in tg.calls)
    rule["value"] = replacement
    changed = await client.put(f"/api/groups/-1001/rules/{row['id']}", json=rule,
        headers={"If-Match": row["version"], "Idempotency-Key": "structured-rule-replace"})
    assert changed.status_code == 200, changed.text
    await service.message(incoming(forms(first)[kind], 803), update_id=8004)
    await service.message(incoming(forms(replacement)[kind], 804), update_id=8005)
    await drain(service)
    deleted = [data["message_id"] for method, data in tg.calls if method == "delete_message"]
    assert deleted == [802, 804]
    row = changed.json()
    disabled = await client.put(f"/api/groups/-1001/rules/{row['id']}", json={**rule, "enabled": False},
        headers={"If-Match": row["version"], "Idempotency-Key": "structured-rule-disable"})
    assert disabled.status_code == 200, disabled.text
    await service.message(incoming(forms(replacement)[kind], 805), update_id=8006)
    await drain(service)
    assert [data["message_id"] for method, data in tg.calls if method == "delete_message"] == deleted
    async with db.sessions() as session:
        archived = await session.scalar(select(ArchivedMessage).where(ArchivedMessage.message_id == 804))
        assert replacement in archived.text
        version = await session.scalar(select(MessageRevision).where(MessageRevision.archive_id == archived.id))
        assert kind.split("_", 1)[0] in version.payload or kind.startswith(("document", "audio", "invoice", "rich"))


async def test_nested_links_ban_using_existing_guard_without_new_keyword_rules(env, monkeypatch):
    from test_advertisement_bans import setup
    await setup(env, monkeypatch)
    _, _, tg, service = env
    msg = incoming(forms("https://t.me/+SyntheticExternal123")["checklist_task"])
    await service.message(msg)
    await drain(service)
    assert any(method == "delete_message" and data["message_id"] == 801 for method, data in tg.calls)
    assert tg.members[(-1001, 42)] == {"status": "kicked", "until_date": 0}


async def test_ai_review_receives_structured_body_and_edits_invalidate_the_old_result(env):
    from test_advertisement_review import reviewer
    _, db, _, service = env
    worker, model = await reviewer(env, lambda row: "模型可见的清单内容" in row["text"])
    msg = incoming(forms("模型可见的清单内容")["checklist_task"])
    await service.message(msg, update_id=8001)
    assert await worker.run_one()
    assert "模型可见的清单内容" in model.calls[0][0]["text"]
    edited = incoming(forms("已经替换成普通内容")["checklist_task"])
    edited["edit_date"] = msg["date"] + 1
    await service.message(edited, edited=True, update_id=8002)
    await worker.run_one()
    async with db.sessions() as session:
        reviews = (await session.scalars(select(AdvertisementReview).order_by(AdvertisementReview.created_at))).all()
        assert reviews[0].state == "superseded" and reviews[-1].state == "clean"


async def test_custom_emoji_pack_inside_checklist_is_resolved_and_uses_rule_disposition(client, env, monkeypatch):
    _, db, tg, service = env
    result = await client.post("/api/groups/-1001/rules", json={"kind": "block_emoji_pack", "value": "SyntheticPack", "penalty": "delete"},
        headers={"Idempotency-Key": "structured-pack-create"})
    assert result.status_code == 200, result.text
    original = tg.call

    async def call(method, **kwargs):
        if method == "get_custom_emoji_stickers":
            return [{"custom_emoji_id": value, "set_name": "SyntheticPack"} for value in kwargs["custom_emoji_ids"]]
        return await original(method, **kwargs)

    monkeypatch.setattr(tg, "call", call)
    msg = incoming({"checklist": {"title": "清单", "tasks": [{"id": 1, "text": "⭐", "text_entities": [
        {"type": "custom_emoji", "offset": 0, "length": 1, "custom_emoji_id": "123456789"}]}]}})
    await service.message(msg)
    await drain(service)
    assert any(method == "delete_message" for method, _ in tg.calls)
    assert not any(method in ("ban_chat_member", "restrict_chat_member") for method, _ in tg.calls)


def test_rich_buttons_links_copy_and_embedded_media_do_not_disappear():
    msg = incoming({"rich_message": {"blocks": [
        {"type": "paragraph", "text": [{"type": "button", "button": {"text": "入口", "url": "https://example.org/path"}}]},
        {"type": "document", "document": {**FILE, "file_name": "可见文件名"}, "caption": {"text": "说明"}},
        {"type": "buttons", "buttons": [{"text": "复制", "copy_text": {"text": "复制的内容"}}]}]}})
    assert has_inline_buttons(msg)
    assert len(inline_buttons(msg)) == 2
    assert "https://example.org/path" in message_content(msg)[1]
    assert all(value in message_text(msg) for value in ("可见文件名", "说明", "入口", "复制的内容"))


def addition(kind, value, original_id=701):
    target = incoming(forms("普通事项")["checklist_task" if kind == "checklist" else "poll_option"], original_id)
    target["from"]["id"] = 43
    if kind == "checklist":
        task = {"id": 2, "text": value}
        target["checklist"]["tasks"].append(task)
        return incoming({"checklist_tasks_added": {"checklist_message": target, "tasks": [task]}})
    option = {"persistent_id": "added", "text": value, "voter_count": 0}
    target["poll"]["options"].append(option)
    return incoming({"poll_option_added": {"poll_message": target, "option_persistent_id": "added", "option_text": value}})


@pytest.mark.parametrize("kind", ["checklist", "poll"])
async def test_additions_are_attributed_to_the_actor_and_remove_the_original_ad_carrier(client, env, kind):
    term = "琥珀银河新增内容"
    result = await client.post("/api/groups/-1001/rules", json={"kind": "keyword", "value": term, "penalty": "ban"},
                              headers={"Idempotency-Key": "structured-addition-rule"})
    assert result.status_code == 200, result.text
    _, _, tg, service = env
    msg = addition(kind, term)
    assert term in message_text(msg) and "普通事项" not in message_text(msg)
    await service.message(msg, update_id=8101)
    await drain(service)
    assert {data["message_id"] for method, data in tg.calls if method == "delete_message"} == {701, 801}
    assert [data["user_id"] for method, data in tg.calls if method == "ban_chat_member"] == [42]
    await service.message(msg, update_id=8101)
    await drain(service)
    assert len([method for method, _ in tg.calls if method == "delete_message"]) == 2


@pytest.mark.parametrize("kind", ["checklist", "poll"])
@pytest.mark.parametrize("archive_enabled", [True, False])
async def test_addition_withdrawn_before_enforcement_cancels_old_decisions_even_without_archive(client, env, kind, archive_enabled):
    term = "青蓝星际新增内容"
    result = await client.post("/api/groups/-1001/rules", json={"kind": "keyword", "value": term, "penalty": "ban"},
                              headers={"Idempotency-Key": "structured-addition-rule"})
    assert result.status_code == 200, result.text
    _, db, tg, service = env
    async with db.transaction() as session:
        group = await session.get(Group, -1001)
        group.settings = {**group.settings, "archive_enabled": archive_enabled}
    msg = addition(kind, term)
    await service.message(msg, update_id=8101)
    target = incoming(forms("普通事项")["checklist_task" if kind == "checklist" else "poll_option"], 701)
    target["from"]["id"] = 43
    target["edit_date"] = msg["date"] + 1
    await service.message(target, edited=True, update_id=8102)
    await drain(service)
    assert not any(method in ("delete_message", "ban_chat_member", "restrict_chat_member") for method, _ in tg.calls)


@pytest.mark.parametrize("kind", ["checklist", "poll"])
async def test_addition_does_not_borrow_the_original_authors_ad_text(client, env, kind):
    term = "青蓝星际测试内容"
    result = await client.post("/api/groups/-1001/rules", json={"kind": "keyword", "value": term, "penalty": "ban"},
                              headers={"Idempotency-Key": "structured-addition-rule"})
    assert result.status_code == 200, result.text
    _, _, tg, service = env
    msg = addition(kind, "新增普通事项")
    event = msg["checklist_tasks_added" if kind == "checklist" else "poll_option_added"]
    if kind == "checklist":
        event["checklist_message"]["checklist"]["title"] = term
    else:
        event["poll_message"]["poll"]["question"] = term
    assert term not in message_text(msg)
    await service.message(msg)
    await drain(service)
    assert not any(method in ("delete_message", "ban_chat_member", "restrict_chat_member") for method, _ in tg.calls)


async def test_ai_review_of_new_checklist_items_also_cleans_the_carrier(env):
    from test_advertisement_review import reviewer
    _, _, tg, service = env
    worker, model = await reviewer(env, lambda row: "只审查这段新增内容" in row["text"])
    await service.message(addition("checklist", "只审查这段新增内容"), update_id=8201)
    assert await worker.run_one()
    assert model.calls[0][0]["text"] == "只审查这段新增内容"
    await drain(service)
    assert {data["message_id"] for method, data in tg.calls if method == "delete_message"} == {701, 801}


def test_rich_quote_credits_and_ordered_list_labels_are_visible():
    msg = incoming({"rich_message": {"blocks": [
        {"type": "pullquote", "text": "引用", "credit": {"type": "bold", "text": "可见落款"}},
        {"type": "list", "items": [{"type": "1", "label": "可见标签", "blocks": []}]}]}})
    assert all(value in message_text(msg) for value in ("引用", "可见落款", "可见标签"))


@pytest.mark.parametrize("kind", ["checklist_task", "poll_option", "document_name", "rich_paragraph"])
async def test_edited_away_structured_content_remains_searchable_and_readable_in_archive(client, env, kind):
    _, _, _, service = env
    term = "归档可见的任意旧内容"
    original = incoming(forms(term)[kind])
    await service.message(original, update_id=8301)
    changed = incoming(forms("当前普通内容")[kind])
    changed["edit_date"] = original["date"] + 1
    await service.message(changed, edited=True, update_id=8302)
    result = await client.get("/api/groups/-1001/messages", params={"query": term})
    assert result.status_code == 200, result.text
    assert result.json()["total"] == 1
    row = result.json()["items"][0]
    assert "当前普通内容" in row["text"]
    detail = (await client.get(f"/api/groups/-1001/messages/{row['id']}")).json()
    assert term in detail["revisions"][0]["display_text"]
    assert message_text(detail["revisions"][0]["payload"]) == detail["revisions"][0]["display_text"]


async def test_structured_messages_follow_durable_sdk_ingress_and_edit_routes(client, env):
    term = "SDK路由测试内容"
    result = await client.post("/api/groups/-1001/rules", json={"kind": "keyword", "value": term, "penalty": "delete"},
                              headers={"Idempotency-Key": "structured-ingress-rule"})
    assert result.status_code == 200, result.text
    _, db, tg, service = env
    runtime = Runtime(db, tg, service)
    try:
        original = incoming(forms("正常清单")["checklist_task"])
        changed = incoming(forms(term)["checklist_task"])
        changed["edit_date"] = original["date"] + 1
        for identifier, field, payload in [(8401, "message", original), (8402, "edited_message", changed)]:
            update = telegram_data(Update.model_validate({"update_id": identifier, field: payload}))
            await runtime.persist([update])
            assert await runtime.process_one()
        await drain(service)
        assert [data["message_id"] for method, data in tg.calls if method == "delete_message"] == [801]
    finally:
        await runtime.stop()


def test_preview_link_and_poll_embedded_media_are_actual_message_content():
    url = "https://t.me/+SyntheticPreview12"
    msg = incoming({"text": "说明", "link_preview_options": {"url": url}})
    assert message_content(msg)[1] == [url]
    hidden = incoming({"text": "说明", "link_preview_options": {"url": url, "is_disabled": True}})
    assert not message_content(hidden)[1]
    p = poll("问题")
    p["options"][0]["media"] = {"document": {**FILE, "file_name": url}}
    assert message_content(incoming({"poll": p}))[1] == [url]


async def test_confirmed_samples_match_structured_content_without_a_model_request(env):
    from test_advertisement_review import add_sample
    _, db, tg, service = env
    msg = incoming(forms("琥珀色纸飞机掠过青蓝山川，随后沿着遥远的河流飞向安静的小岛")["checklist_task"])
    sample = await add_sample(env, message_text(msg))
    await service.message(msg, update_id=8501)
    async with db.sessions() as session:
        review = await session.scalar(select(AdvertisementReview))
        assert review.state == "matched" and review.result["sample_id"] == sample["id"]
    await drain(service)
    assert any(method == "delete_message" and data["message_id"] == 801 for method, data in tg.calls)
