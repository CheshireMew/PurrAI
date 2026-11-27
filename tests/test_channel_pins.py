import pytest
from sqlalchemy import select

from conftest import drain, message
from purrai.models import Action, Group
from purrai.service import Service
from purrai.update_receiver import UpdateReceiver


def forward(identifier=81, **changes):
    return message(identifier, "关联频道的新帖子", is_automatic_forward=True,
                   sender_chat={"id": -2001, "type": "channel", "title": "测试频道"}, **changes)


def manual_pin(target, *, anonymous=False):
    data = message(91, "", pinned_message=target)
    data["from"] = {"id": 7, "is_bot": False}
    if anonymous:
        data["sender_chat"] = {"id": -1001, "type": "supergroup"}
    return data


async def configure(env, client, monkeypatch, *, can_pin=True):
    _, _, tg, _ = env
    original = tg.call
    state = {"can_pin": can_pin, "linked": -2001}

    async def call(method, **kwargs):
        result = await original(method, **kwargs)
        if method == "get_chat" and kwargs["chat_id"] == -1001:
            result.update(id=-1001, linked_chat_id=state["linked"])
        if method == "get_chat_member" and kwargs["user_id"] == tg.user_id:
            result["can_pin_messages"] = state["can_pin"]
        return result

    monkeypatch.setattr(tg, "call", call)
    response = await client.patch("/api/groups/-1001/settings", json={
        "settings": {"linked_channel_auto_unpin": True}, "request_id": "enable-channel-auto-unpin"})
    assert response.status_code == (200 if can_pin else 422), response.text
    return state


async def action(env):
    async with env[1].sessions() as session:
        return await session.scalar(select(Action).where(Action.kind == "linked_channel_unpin"))


def unpins(env):
    return [kwargs for method, kwargs in env[2].calls if method == "unpin_chat_message"]


async def test_group_setting_is_local_persistent_and_can_be_disabled(client, env, monkeypatch):
    before = (await client.get("/api/settings/defaults")).json()
    assert not before["settings"]["linked_channel_auto_unpin"]
    await configure(env, client, monkeypatch)
    inheritance = (await client.get("/api/groups/-1001/settings/inheritance")).json()
    assert inheritance["settings"]["linked_channel_auto_unpin"]
    assert "linked_channel_auto_unpin" in inheritance["overrides"]
    assert not inheritance["defaults"]["linked_channel_auto_unpin"]
    settings, db, tg, _ = env
    restarted = Service(db, tg, settings)
    await restarted.message(forward())
    await drain(restarted)
    assert unpins(env) == [{"chat_id": -1001, "message_id": 81}]
    response = await client.patch("/api/groups/-1001/settings", json={
        "settings": {"linked_channel_auto_unpin": False}, "request_id": "disable-channel-auto-unpin"})
    assert response.status_code == 200
    await restarted.message(forward(82))
    await drain(restarted)
    assert len(unpins(env)) == 1


async def test_enabling_requires_pin_permission(client, env, monkeypatch):
    await configure(env, client, monkeypatch, can_pin=False)
    group = (await client.get("/api/groups/-1001/settings/inheritance")).json()
    assert not group["settings"]["linked_channel_auto_unpin"]


@pytest.mark.parametrize("change", ["disabled", "paused", "unapproved", "edited", "manual_forward", "direct", "other_channel"])
async def test_only_new_automatic_forwards_from_current_linked_channel_are_unpinned(client, env, monkeypatch, change):
    state = await configure(env, client, monkeypatch)
    settings, db, tg, service = env
    incoming = forward()
    async with db.transaction() as session:
        group = await session.get(Group, -1001)
        if change == "disabled":
            group.settings = {**group.settings, "linked_channel_auto_unpin": False}
        elif change == "paused":
            group.enabled = False
        elif change == "unapproved":
            group.approved = False
    if change in ("manual_forward", "direct"):
        incoming.pop("is_automatic_forward")
    if change == "manual_forward":
        incoming.pop("sender_chat")
        incoming["forward_origin"] = {"type": "channel", "chat": {"id": -2001, "type": "channel"}}
    if change == "other_channel":
        state["linked"] = -3001
    await service.message(incoming, edited=change == "edited")
    await drain(service)
    assert not unpins(env)


@pytest.mark.parametrize("anonymous", [False, True])
@pytest.mark.parametrize("pin_first", [False, True])
async def test_manual_pin_cancels_pending_unpin_even_if_original_is_processed_later(client, env, monkeypatch, anonymous, pin_first):
    await configure(env, client, monkeypatch)
    settings, db, tg, service = env
    receiver = UpdateReceiver(db, tg, settings, {})
    original = forward()
    updates = [{"update_id": 101, "message": original},
               {"update_id": 102, "message": manual_pin(original, anonymous=anonymous)}]
    if pin_first:
        updates.reverse()
    await receiver.persist(updates)
    await service.message(original)
    await drain(service)
    assert not unpins(env)
    assert (await action(env)).state == "cancelled"


async def test_manual_repin_survives_duplicate_forward_and_edit(client, env, monkeypatch):
    await configure(env, client, monkeypatch)
    service = env[3]
    original = forward()
    await service.message(original)
    await drain(service)
    await service.message(manual_pin(original))
    await service.message(original)
    await service.message({**original, "text": "编辑频道帖子", "edit_date": original["date"] + 1}, edited=True)
    await drain(service)
    assert unpins(env) == [{"chat_id": -1001, "message_id": 81}]


@pytest.mark.parametrize("change", ["setting", "manual", "linked", "permission"])
async def test_settings_link_and_manual_pin_are_rechecked_after_network_wait(client, env, monkeypatch, change):
    state = await configure(env, client, monkeypatch)
    _, db, tg, service = env
    await service.message(forward())
    if change in ("linked", "permission"):
        state["linked" if change == "linked" else "can_pin"] = -3001 if change == "linked" else False
    else:
        original = tg.call
        changed = False

        async def call(method, **kwargs):
            nonlocal changed
            result = await original(method, **kwargs)
            if method == "get_chat" and not changed:
                changed = True
                if change == "setting":
                    async with db.transaction() as session:
                        group = await session.get(Group, -1001)
                        group.settings = {**group.settings, "linked_channel_auto_unpin": False}
                else:
                    await UpdateReceiver(db, tg, env[0], {}).persist([
                        {"update_id": 103, "message": manual_pin(forward())}])
            return result

        monkeypatch.setattr(tg, "call", call)
    await drain(service)
    assert not unpins(env)
    assert (await action(env)).state == ("failed" if change == "permission" else "cancelled")


async def test_network_retry_stops_when_user_manually_pins(client, env, monkeypatch):
    await configure(env, client, monkeypatch)
    _, db, tg, service = env
    await service.message(forward())
    tg.fail["unpin_chat_message"] = 1
    assert await service.run_one_action()
    pending = await action(env)
    assert pending.state == "pending" and pending.attempts == 1
    await service.message(manual_pin(forward()))
    async with db.transaction() as session:
        row = await session.get(Action, pending.id)
        row.available_at = 0
    await drain(service)
    assert len(unpins(env)) == 1  # Only the failed attempt; no second unpin.
    assert (await action(env)).state == "cancelled"
