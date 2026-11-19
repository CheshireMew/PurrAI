"""Message custom-emoji sets are editable rules, independent of profile status."""
from uuid import uuid4

import pytest
from sqlalchemy import select

from conftest import drain, message
from purrai.models import Action, Group, RuleHit, Violation


async def save(client, pack, previous=None, **changes):
    body = {"kind": "block_emoji_pack", "value": pack, "scope": "message",
            "penalty": "permanent_mute", **changes}
    headers = {"Idempotency-Key": uuid4().hex}
    path = "/api/groups/-1001/rules"
    if previous:
        headers["If-Match"] = previous["version"]
        response = await client.put(path + "/" + previous["id"], json=body, headers=headers)
    else:
        response = await client.post(path, json=body, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


async def test_configured_pack_ban_also_deletes_the_message(env, client, monkeypatch):
    _, _, tg, service = env
    await save(client, "arbitrary_banned_pack", penalty="ban")
    resolve_with(tg, monkeypatch, {"100": "arbitrary_banned_pack"})
    await service.message(emoji_message())
    await drain(service)
    assert any(method == "delete_message" and args["message_id"] == 10 for method, args in tg.calls)
    assert tg.members[(-1001, 42)] == {"status": "kicked", "until_date": 0}


def emoji_message(identifier=10, emoji="100", **kwargs):
    return message(identifier, "🙂", entities=[{"type": "custom_emoji", "offset": 0, "length": 2,
                                              "custom_emoji_id": emoji}], **kwargs)


def resolve_with(tg, monkeypatch, mapping):
    original, batches = tg.call, []
    async def call(method, **kwargs):
        if method == "get_custom_emoji_stickers":
            batches.append(kwargs["custom_emoji_ids"])
            return [{"custom_emoji_id": item, "set_name": mapping[item]}
                    for item in kwargs["custom_emoji_ids"] if item in mapping]
        return await original(method, **kwargs)
    monkeypatch.setattr(tg, "call", call)
    return batches


async def effects(db):
    async with db.sessions() as session:
        return (await session.scalars(select(Action).where(Action.kind.in_(("delete", "rule_mute"))))).all()


@pytest.mark.parametrize("variant", ["text", "caption", "sticker"])
async def test_message_pack_deletes_and_mutes_without_ai_or_status_rule(env, client, monkeypatch, variant):
    _, db, tg, service = env
    await save(client, "https://t.me/addemoji/Arbitrary_Pack_By_TestBot")
    batches = resolve_with(tg, monkeypatch, {"100": "Arbitrary_Pack_By_TestBot"})
    incoming = emoji_message()
    if variant == "caption":
        incoming["caption"], incoming["caption_entities"] = incoming.pop("text"), incoming.pop("entities")
        incoming["photo"] = [{"file_id": "test"}]
    elif variant == "sticker":
        incoming.pop("text")
        incoming.pop("entities")
        incoming["sticker"] = {"set_name": "Arbitrary_Pack_By_TestBot", "file_id": "test"}
    await service.message(incoming)
    await drain(service)
    assert [args["message_id"] for method, args in tg.calls if method == "delete_message"] == [10]
    assert tg.members[(-1001, 42)]["until_date"] == 0 and not tg.members[(-1001, 42)]["can_send_messages"]
    assert not any(method == "ban_chat_member" for method, _ in tg.calls)
    assert len(batches) == (0 if variant == "sticker" else 1)
    async with db.sessions() as session:
        violation = await session.scalar(select(Violation))
        assert violation.rule_mute and not violation.profile_violation
        assert await session.scalar(select(RuleHit))
        group = await session.get(Group, -1001)
        assert "arbitrary_pack_by_testbot" not in group.settings["emoji_status_blocked_packs"]


async def test_arbitrary_pack_save_edit_disable_through_management_api(env, client, monkeypatch):
    _, db, tg, service = env
    resolve_with(tg, monkeypatch, {"100": "first_pack", "200": "second_pack"})
    first = await save(client, "first_pack")
    await service.message(emoji_message(10))
    assert len(await effects(db)) == 2
    second = await save(client, "second_pack", previous=first)
    await drain(service)  # Changed rules cancel both pending effects from the first value.
    assert not any(method in ("delete_message", "restrict_chat_member") for method, _ in tg.calls)
    await service.message(emoji_message(11, user_id=43))
    assert len(await effects(db)) == 2
    await service.message(emoji_message(12, emoji="200", user_id=44))
    assert len(await effects(db)) == 4
    await save(client, "second_pack", previous=second, enabled=False)
    await drain(service)
    await service.message(emoji_message(13, emoji="200", user_id=45))
    assert len(await effects(db)) == 4
    assert not any(method in ("delete_message", "restrict_chat_member") for method, _ in tg.calls)


async def test_all_custom_emoji_are_batched_cached_and_only_own_content_counts(env, client, monkeypatch):
    _, _, tg, service = env
    await save(client, "sample_pack")
    batches = resolve_with(tg, monkeypatch, {"100": "other_pack", "200": "sample_pack"})
    incoming = emoji_message()
    incoming["entities"].append({"type": "custom_emoji", "offset": 2, "length": 2, "custom_emoji_id": "200"})
    incoming["text"] += "🙂"
    await service.message(incoming)
    await service.message({**incoming, "message_id": 11})
    assert batches == [["100", "200"]]
    tg.calls.clear()
    own = message(12, "引用这条消息询问情况", user_id=43, reply_to_message=incoming)
    own["from"].update(emoji_status_custom_emoji_id="200", first_name="sample_pack")
    await service.message(own)
    assert len(batches) == 1
    assert not any(row.target_message_id == 12 for row in await effects(env[1]))


async def test_state_blacklist_does_not_block_same_pack_in_message(env, client, monkeypatch):
    _, db, tg, service = env
    async with db.transaction() as session:
        group = await session.get(Group, -1001)
        group.settings = {**group.settings, "emoji_status_blocked_packs": ["status_only"]}
    batches = resolve_with(tg, monkeypatch, {"100": "status_only"})
    await service.message(emoji_message())
    await drain(service)
    assert batches == [] and not await effects(db)


async def test_lookup_failure_retries_without_false_pass_or_punishment(env, client, monkeypatch):
    _, db, tg, service = env
    await save(client, "sample_pack")
    mapping = {}
    batches = resolve_with(tg, monkeypatch, mapping)
    with pytest.raises(ConnectionError):
        await service.message(emoji_message())
    assert not await effects(db)
    mapping["100"] = "sample_pack"
    await service.message(emoji_message())
    await drain(service)
    assert len(batches) == 2 and any(method == "delete_message" for method, _ in tg.calls)


@pytest.mark.parametrize("archive", [True, False])
async def test_edit_removes_pack_and_cancels_pending_delete_and_mute(env, client, monkeypatch, archive):
    _, db, tg, service = env
    await save(client, "sample_pack")
    resolve_with(tg, monkeypatch, {"100": "sample_pack"})
    async with db.transaction() as session:
        group = await session.get(Group, -1001)
        group.settings = {**group.settings, "archive_enabled": archive}
    original = emoji_message()
    await service.message(original, update_id=1)
    updated = {**original, "entities": [], "text": "已经改成普通消息", "edit_date": original["date"] + 1}
    await service.message(updated, True, update_id=2)
    await drain(service)
    assert not any(method in ("delete_message", "restrict_chat_member") for method, _ in tg.calls)


async def test_configured_delete_only_and_preview_do_not_mute(env, client, monkeypatch):
    _, _, tg, service = env
    await save(client, "sample_pack", penalty="delete")
    preview = await client.post("/api/groups/-1001/rules/preview", json={"text": "", "message_emoji_packs": ["https://t.me/addemoji/sample_pack"]})
    assert preview.status_code == 200, preview.text
    assert preview.json()["delete_message"] and not preview.json()["mute_member"]
    resolve_with(tg, monkeypatch, {"100": "sample_pack"})
    await service.message(emoji_message())
    await drain(service)
    assert any(method == "delete_message" for method, _ in tg.calls)
    assert not any(method == "restrict_chat_member" for method, _ in tg.calls)


async def test_given_pack_is_plain_rule_data(env, client, monkeypatch):
    # The user's actual package occurs only in a test fixture, never in the engine/seed.
    pack = "zk7162629_1794146550_1_by_c777zt_bot"
    saved = await save(client, "https://t.me/addemoji/" + pack)
    resolve_with(env[2], monkeypatch, {"6016826844676891270": pack})
    await env[3].message(emoji_message(10, "6016826844676891270"))
    await drain(env[3])
    assert saved["value"] == pack
    assert any(method == "delete_message" for method, _ in env[2].calls)
    assert env[2].members[(-1001, 42)]["until_date"] == 0


@pytest.mark.parametrize("exemption", ["administrator", "whitelist"])
async def test_message_pack_keeps_existing_exemptions(env, client, monkeypatch, exemption):
    from purrai.models import Whitelist
    _, db, tg, service = env
    await save(client, "sample_pack")
    resolve_with(tg, monkeypatch, {"100": "sample_pack"})
    if exemption == "administrator":
        tg.members[(-1001, 42)] = {"status": "administrator"}
    else:
        async with db.transaction() as session:
            session.add(Whitelist(group_id=-1001, user_id=42, moderation=True, verification=True))
    await service.message(emoji_message())
    await drain(service)
    assert not any(method in ("delete_message", "restrict_chat_member") for method, _ in tg.calls)


async def test_shared_rule_can_be_saved_replaced_and_disabled(client):
    baseline = (await client.get("/api/settings/defaults")).json()
    values = {"kind": "block_emoji_pack", "value": "shared_pack_one", "penalty": "permanent_mute"}
    for value, enabled in (("shared_pack_one", True), ("shared_pack_two", True), ("shared_pack_two", False)):
        rules = [*baseline["rules"]]
        previous = next((row for row in rules if row["kind"] == "block_emoji_pack"), None)
        rules = [row for row in rules if row is not previous]
        rules.append({**(previous or {}), **values, "value": value, "enabled": enabled})
        response = await client.patch("/api/settings/defaults", json={"baseline": baseline, "rules": rules,
                                                                     "request_id": uuid4().hex})
        assert response.status_code == 200, response.text
        baseline = response.json()
        for pack in ("shared_pack_one", "shared_pack_two"):
            preview = await client.post("/api/groups/-1001/rules/preview", json={"text": "", "message_emoji_packs": [pack]})
            assert preview.json()["mute_member"] == (pack == value and enabled)


async def test_archive_preview_marks_unknown_sources_then_uses_saved_resolution(env, client, monkeypatch):
    from purrai.message_emoji import resolve
    from purrai.models import ArchivedMessage
    _, db, tg, service = env
    incoming = emoji_message()
    await service.message(incoming)
    batches = resolve_with(tg, monkeypatch, {"100": "preview_pack"})
    async with db.sessions() as session:
        archive = await session.scalar(select(ArchivedMessage))
        identifier = archive.id
    body = {"archive_ids": [identifier], "draft": {"kind": "block_emoji_pack", "value": "preview_pack",
                                                     "penalty": "permanent_mute"}}
    result = await client.post("/api/groups/-1001/rules/preview-archive", json=body)
    assert result.status_code == 200, result.text
    assert result.json()["items"][0]["unresolved_emoji"] and not batches
    await resolve(db, tg, incoming)
    result = await client.post("/api/groups/-1001/rules/preview-archive", json=body)
    row = result.json()["items"][0]
    assert row["after"]["mute_member"] and row["after"]["delete_message"] and not row["unresolved_emoji"]
    assert not await effects(db)
