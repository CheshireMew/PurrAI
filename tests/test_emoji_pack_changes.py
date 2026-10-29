import asyncio

import pytest
from sqlalchemy import select

from purrai.management import Management, ManagementError
from purrai.models import Action, Group
from purrai.schemas import EmojiPackChange


async def valid_packs(tg, monkeypatch):
    original = tg.call

    async def call(method, **kwargs):
        if method == "get_sticker_set":
            return {"name": kwargs["name"], "sticker_type": "custom_emoji"}
        return await original(method, **kwargs)

    monkeypatch.setattr(tg, "call", call)


async def packs(db):
    async with db.sessions() as session:
        return (await session.get(Group, -1001)).settings["emoji_status_blocked_packs"]


async def test_add_is_incremental_deduplicated_and_replay_does_not_restore_removed_pack(
    env, monkeypatch, client
):
    _, db, tg, _ = env
    await valid_packs(tg, monkeypatch)
    path = "/api/groups/-1001/emoji-status-packs"
    body = {"packs": ["https://t.me/addemoji/NewPack", "NEWPACK"], "request_id": "new-pack-123"}
    response = await client.post(path, json=body)
    assert response.status_code == 200
    result = response.json()
    assert result["packs"] == ["bygbb666", "gshechaba", "tgvip_16", "newpack"]
    assert result["added"] == ["newpack"]
    response = await client.post(path, json={"packs": ["NEWPACK"], "request_id": "duplicate-123"})
    assert response.status_code == 200 and response.json()["added"] == []
    response = await client.post(
        path, json={"operation": "remove", "packs": ["NewPack"], "request_id": "remove-123"}
    )
    assert response.status_code == 200 and response.json()["removed"] == ["newpack"]
    assert (await client.post(path, json=body)).json() == result
    assert await packs(db) == ["bygbb666", "gshechaba", "tgvip_16"]
    assert (await client.post(path, json={**body, "packs": ["otherpack"]})).status_code == 409
    async with db.sessions() as session:
        changes = (await session.scalars(select(Action))).all()
        assert len(changes) == 3 and all(
            row.kind == "settings" and row.state == "succeeded" for row in changes
        )


async def test_concurrent_adds_merge_under_lock_and_preserve_other_settings(env, monkeypatch):
    settings, db, tg, svc = env
    entered, release = asyncio.Event(), asyncio.Event()
    original = tg.call
    count = 0

    async def call(method, **kwargs):
        nonlocal count
        if method == "get_sticker_set":
            count += 1
            if count == 2:
                entered.set()
            await release.wait()
            return {"name": kwargs["name"], "sticker_type": "custom_emoji"}
        return await original(method, **kwargs)

    monkeypatch.setattr(tg, "call", call)
    management = Management(db, tg, svc, settings)
    tasks = [
        asyncio.create_task(
            management.change_status_packs(
                1, -1001, EmojiPackChange(packs=[name], request_id="concurrent-" + name)
            )
        )
        for name in ("new_a", "new_b")
    ]
    try:
        await asyncio.wait_for(entered.wait(), 3)
        async with db.transaction() as session:
            group = await session.get(Group, -1001)
            group.settings = {**group.settings, "repeat_count": 9}
    finally:
        release.set()
        await asyncio.gather(*tasks)
    assert set(await packs(db)) == {"bygbb666", "gshechaba", "tgvip_16", "new_a", "new_b"}
    async with db.sessions() as session:
        assert (await session.get(Group, -1001)).settings["repeat_count"] == 9


async def test_remove_and_clear_work_without_fetching_deleted_or_unavailable_pack(env, monkeypatch, client):
    _, db, tg, _ = env

    async def offline(*args, **kwargs):
        raise ConnectionError("offline")

    monkeypatch.setattr(tg, "call", offline)
    path = "/api/groups/-1001/emoji-status-packs"
    response = await client.post(
        path, json={"operation": "remove", "packs": ["bygbb666"], "request_id": "remove-offline"}
    )
    assert response.status_code == 200 and await packs(db) == ["gshechaba", "tgvip_16"]
    response = await client.post(path, json={"operation": "clear", "request_id": "clear-offline"})
    assert response.status_code == 200 and await packs(db) == []


@pytest.mark.parametrize(
    "body",
    [
        {"packs": []},
        {"packs": ["@publisher"]},
        {"packs": ["https://t.me/addstickers/TestPack"]},
        {"operation": "clear", "packs": ["bygbb666"]},
        {"operation": "replace", "packs": ["testpack"]},
    ],
)
async def test_invalid_change_does_not_modify_list(env, client, body):
    response = await client.post(
        "/api/groups/-1001/emoji-status-packs", json={**body, "request_id": "invalid-request"}
    )
    assert response.status_code == 422
    assert await packs(env[1]) == ["bygbb666", "gshechaba", "tgvip_16"]


async def test_failed_add_does_not_modify_list_and_capacity_never_evicts_entries(env, monkeypatch, client):
    _, db, tg, _ = env
    path = "/api/groups/-1001/emoji-status-packs"

    async def offline(*args, **kwargs):
        raise ConnectionError("offline")

    monkeypatch.setattr(tg, "call", offline)
    response = await client.post(path, json={"packs": ["anotherpack"], "request_id": "offline-request"})
    assert response.status_code == 503 and await packs(db) == ["bygbb666", "gshechaba", "tgvip_16"]
    await valid_packs(tg, monkeypatch)
    full = [f"pack_{n}" for n in range(100)]
    async with db.transaction() as session:
        group = await session.get(Group, -1001)
        group.settings = {**group.settings, "emoji_status_blocked_packs": full}
    response = await client.post(path, json={"packs": ["one_more"], "request_id": "full-request"})
    assert response.status_code == 422 and await packs(db) == full


async def test_only_current_group_admin_can_change_list(env, monkeypatch, client):
    settings, db, tg, svc = env
    await valid_packs(tg, monkeypatch)
    management = Management(db, tg, svc, settings)
    change = EmojiPackChange(packs=["anotherpack"], request_id="denied-request")
    with pytest.raises(ManagementError) as error:
        await management.change_status_packs(42, -1001, change)
    assert error.value.status == 403
    async with db.transaction() as session:
        (await session.get(Group, -1001)).approved = False
    response = await client.post("/api/groups/-1001/emoji-status-packs", json=change.model_dump())
    assert response.status_code == 403 and await packs(db) == ["bygbb666", "gshechaba", "tgvip_16"]


async def test_bot_list_pages_and_long_existing_list_do_not_need_resubmission(env):
    from purrai.runtime import Runtime
    from test_bot_admin import click, markup

    _, db, tg, svc = env
    full = [f"pack_{n}_" + "x" * 50 for n in range(100)]
    async with db.transaction() as session:
        group = await session.get(Group, -1001)
        group.settings = {**group.settings, "emoji_status_blocked_packs": full}
    runtime = Runtime(db, tg, svc)
    await runtime.admin.emoji_packs_panel(1, -1001)
    assert full[0] in markup(tg)["text"] and full[10] not in markup(tg)["text"]
    await click(runtime, tg, "下一页")
    assert full[10] in markup(tg)["text"] and full[0] not in markup(tg)["text"]
    await click(runtime, tg, "新增表情包")
    prompt = next(values["text"] for method, values in reversed(tg.calls) if method == "send_message")
    assert len(prompt) < 4000 and "当前已有 100 个" in prompt
    assert await packs(db) == full
