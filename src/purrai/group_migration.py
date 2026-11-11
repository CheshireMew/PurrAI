"""Atomic continuity for Telegram's group-to-supergroup service messages."""

import json
import secrets

from sqlalchemy import select, update

from purrai.action_queue import enqueue
from purrai.models import Base, Group, Meta, Action, Inbox, Verification, now


async def receive_migration(session, payload):
    message = payload.get("message", {})
    chat = message.get("chat", {})
    if message.get("migrate_to_chat_id"):
        old, new = chat.get("id"), message["migrate_to_chat_id"]
    elif message.get("migrate_from_chat_id"):
        old, new = message["migrate_from_chat_id"], chat.get("id")
    else:
        return False
    if not all(type(value) is int and value < 0 for value in (old, new)) or old == new:
        return True
    key = f"group-migration:{new}"
    previous = await session.get(Meta, key)
    if previous:
        if json.loads(previous.value)["old_id"] != old:
            raise ValueError("群升级来源与已保存记录不一致")
        return True
    origin = await session.get(Group, old)
    if not origin or not origin.approved:
        return True
    target = await session.get(Group, new)
    if target and target.approved:
        raise ValueError("升级目标已独立接入，请先核实群升级来源，未覆盖任何数据")
    # An unapproved discovered target may be reused, but never merge independently
    # managed member/message namespaces.
    for table in Base.metadata.sorted_tables:
        if "group_id" in table.c and await session.scalar(select(table.c.group_id).where(table.c.group_id == new).limit(1)):
            raise ValueError("升级目标已有独立记录，未覆盖任何数据")
    if target is None:
        target = Group(id=new)
        session.add(target)
    for column in Group.__table__.columns:
        if column.name not in ("id", "type", "title"):
            setattr(target, column.name, getattr(origin, column.name))
    target.type, target.title = "supergroup", chat.get("title") or origin.title
    await session.flush()
    # Telegram starts a new message-number space. Negative historical IDs are
    # internal references only; original IDs are presented separately below.
    for table in Base.metadata.sorted_tables:
        if "group_id" not in table.c:
            continue
        values = {"group_id": new}
        if "message_id" in table.c and table.name != "verifications":
            await session.execute(update(table).where(table.c.group_id == old, table.c.message_id > 0)
                                  .values(message_id=-table.c.message_id))
        await session.execute(update(table).where(table.c.group_id == old).values(**values))
    # Keep the old group as a non-authorized tombstone so stale events and buttons
    # cannot revive protection under the obsolete ID.
    origin.approved, origin.enabled, origin.status = False, False, "migrated"
    for action in await session.scalars(select(Action).where(Action.group_id == new)):
        action.payload = {**action.payload, "origin_group_id": old}
        if action.state == "pending" and (action.payload.get("message_id") or action.kind in ("verify_mute", "challenge", "progress", "archive_export")):
            action.state, action.error = "cancelled", "群已升级，原群消息编号不能用于新群"
    for verification in await session.scalars(select(Verification).where(Verification.group_id == new)):
        if verification.prompt_chat_id == old:
            verification.prompt_chat_id = None
        if verification.flow == "joined":
            verification.message_id = None
            if verification.state in ("muting", "pending"):
                verification.nonce = secrets.token_hex(4)
                verification.answer_hash, verification.input_digits = "", ""
                await enqueue(session, "verify_mute", f"migrate-mute:{verification.id}:{new}", new,
                              verification.user_id, {"verification_id": verification.id}, priority=0)
    # Old retained chat content is history, not replyable context in the new chat.
    from purrai.models import ChatMessage, ChatTurn
    await session.execute(update(ChatMessage).where(ChatMessage.group_id == new).values(eligible=False))
    await session.execute(update(ChatTurn).where(ChatTurn.group_id == new, ChatTurn.state.in_(
        ("pending", "generating", "sending"))).values(state="skipped", error="群升级，已停止旧群回复"))
    for row in await session.scalars(select(Inbox).where(Inbox.state == "pending")):
        if any(row.payload.get(field, {}).get("chat", {}).get("id") == old for field in (
                "message", "edited_message", "chat_member", "my_chat_member", "chat_join_request")):
            row.state, row.completed_at, row.error = "done", now(), "群升级，旧群事件已归档并停止重放"
            row.payload = {"update_id": row.id}
    # Only exact group-key components are moved; bot drafts keep their old scope
    # and fail authorization instead of being silently redirected.
    prefixes = ("group-links:", "group-linked-channel:", "group-links-backfilled:",
                "archive:quota:", "archive:group-bytes:", "chat_input:", "chat_stickers:",
                "chat_attention:", "chat_reset:", "chat_session:", "text-history:", "chat-usage:", "member-decision:")
    for row in (await session.scalars(select(Meta))).all():
        if row.key.startswith(prefixes) and str(old) in row.key.split(":"):
            replacement = ":".join(str(new) if part == str(old) else part for part in row.key.split(":"))
            if not await session.get(Meta, replacement):
                value = row.value
                if row.key.startswith("chat-usage:"):
                    value = json.dumps({**json.loads(value), "group_id": new})
                session.add(Meta(key=replacement, value=value, expires_at=row.expires_at))
            await session.delete(row)
    budget = await session.get(Meta, "private:chat-budget")
    if budget:
        data = json.loads(budget.value)
        if str(old) in data.get("groups", {}):
            data["groups"][str(new)] = dict(data["groups"][str(old)])
            budget.value = json.dumps(data)
    reset = await session.get(Meta, f"chat_reset:{new}")
    if reset:
        reset.value = str(now())
    else:
        session.add(Meta(key=f"chat_reset:{new}", value=str(now())))
    data = {"old_id": old, "new_id": new, "at": now()}
    session.add(Meta(key=key, value=json.dumps(data)))
    await enqueue(session, "group_migration", f"group-migration:{old}:{new}", new, payload={"result": data})
    audit = await session.scalar(select(Action).where(Action.dedupe_key == f"group-migration:{old}:{new}"))
    audit.state = "succeeded"
    return True
