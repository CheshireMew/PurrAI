"""Durable group messages, revisions and moderation outcomes. No media downloads."""

import hashlib
import json

from sqlalchemy import and_, func, or_, select

from purrai.models import (
    Action,
    ArchivedMessage,
    Group,
    MessageRevision,
    Meta,
    Violation,
    now,
    uid,
)
from purrai.schemas import ArchiveFilter
from purrai.serialization import row_dict as values
from purrai.sender_identity import sender_name
from purrai.archive_capacity import group_bytes, increment, limits

ARCHIVE_BYTES = "archive:stored-bytes"


def visible_bytes(row):
    return sum(len((getattr(row, field, "") or "").encode("utf-8"))
               for field in ("text", "caption", "sender_name", "username", "deletion_error")) + 512


async def archive_bytes(session):
    row = await session.get(Meta, ARCHIVE_BYTES)
    if row:
        return row, int(row.value)
    from sqlalchemy import text
    revisions = await session.scalar(text(
        "SELECT coalesce(sum(length(cast(payload AS BLOB))+length(cast(reasons AS BLOB))+512),0) FROM message_revisions"))
    messages = await session.scalar(text(
        "SELECT coalesce(sum(length(cast(text AS BLOB))+length(cast(caption AS BLOB))+"
        "length(cast(sender_name AS BLOB))+length(cast(username AS BLOB))+length(cast(deletion_error AS BLOB))+512),0) FROM archived_messages"))
    return None, revisions + messages


MEDIA = (
    "photo",
    "video",
    "animation",
    "document",
    "audio",
    "voice",
    "video_note",
    "sticker",
    "contact",
    "location",
    "venue",
    "poll",
    "dice",
    "story",
    "paid_media",
)




def snapshot(message):
    # A reply/pin may embed an old message. Keep its reference, never claim it was observed itself.
    data = {
        key: value
        for key, value in message.items()
        if key not in ("reply_markup", "reply_to_message", "pinned_message")
    }
    for key in ("reply_to_message", "pinned_message"):
        if message.get(key):
            data[key] = {"message_id": message[key]["message_id"]}
    return data


def version_key(message, edited, update_id):
    if update_id:
        return f"update:{update_id}"
    encoded = json.dumps(
        [edited, snapshot(message)], ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return "hash:" + hashlib.sha256(encoded.encode()).hexdigest()


async def recording_status(session, group, settings=None):
    if not group or not group.approved or not group.settings.get("archive_enabled", True):
        return {"state": "disabled", "reason": ""}
    _, used = await archive_bytes(session)
    limit, group_limit = await limits(session, settings, group.id)
    _, group_used = await group_bytes(session, group.id)
    size = {"bytes": used, "limit_bytes": limit, "group_bytes": group_used, "group_limit_bytes": group_limit}
    if used >= limit:
        return {"state": "capacity_paused", "reason": "已保留的归档数据达到容量上限，已暂停新增归档；已有记录保留", **size}
    if group_limit is not None and group_used >= group_limit:
        return {"state": "capacity_paused", "reason": "本群归档已达到分配额度，已暂停新增归档；其他群可继续记录", **size}
    return {"state": "active", "reason": "", **size}


async def record_gap(session, row, message, edited, update_id, reason):
    """Keep bounded, content-free evidence that this is an incomplete archive."""
    if row is None:
        return
    key = f"archive:gap:{row.id}"
    marker = await session.get(Meta, key)
    data = json.loads(marker.value) if marker else {}
    order = [message.get("edit_date") or message["date"], bool(edited), update_id]
    version = version_key(message, edited, update_id)
    if data.get("version") == version or data.get("order", []) >= order:
        return
    data = {"omitted_at_least": data.get("omitted_at_least", 0) + 1,
            "first_skipped_at": data.get("first_skipped_at", now()), "last_skipped_at": now(),
            "reason": reason, "version": version, "order": order}
    if marker:
        marker.value = json.dumps(data)
    else:
        session.add(Meta(key=key, value=json.dumps(data)))


async def capture(session, message, edited=False, update_id=0, *, settings=None):
    chat = message.get("chat", {})
    if chat.get("type") not in ("group", "supergroup") or message.get("message_id", 0) <= 0:
        return
    group = await session.get(Group, chat["id"])
    if group is None or not group.approved:
        return
    status = await recording_status(session, group, settings)
    if status["state"] == "disabled":
        return
    row = await session.scalar(
        select(ArchivedMessage).where(
            ArchivedMessage.group_id == group.id, ArchivedMessage.message_id == message["message_id"]
        )
    )
    key = version_key(message, edited, update_id)
    if row and await session.scalar(select(MessageRevision.id).where(
        MessageRevision.archive_id == row.id, MessageRevision.version_key == key,
    )):
        return
    marker = await session.get(Meta, "archive:paused")
    if status["state"] == "capacity_paused":
        if not marker and status["bytes"] >= status["limit_bytes"]:
            session.add(Meta(key="archive:paused", value=status["reason"]))
        await record_gap(session, row, message, edited, update_id, status["reason"])
        return
    if marker:
        await session.delete(marker)
    if row and row.revision_count >= getattr(settings, "archive_max_revisions", 20):
        await record_gap(session, row, message, edited, update_id, "已达到单条消息版本上限")
        return
    counter, used = await archive_bytes(session)
    group_counter, group_used = await group_bytes(session, group.id)
    before = visible_bytes(row) if row else 0
    event_at = message.get("edit_date") or message["date"]
    if row is None:
        row = ArchivedMessage(
            id=uid(),
            group_id=group.id,
            message_id=message["message_id"],
            sent_at=message["date"],
            original_observed=not edited,
            revision_count=0,
        )
        session.add(row)
        await session.flush()
    session.add(
        MessageRevision(
            archive_id=row.id,
            version_key=key,
            update_id=update_id,
            is_edit=edited,
            event_at=event_at,
            payload=snapshot(message),
        )
    )
    row.revision_count += 1
    row.original_observed = row.original_observed or not edited
    row.updated_at = now()
    # Replayed originals or delayed older edits must not replace the latest visible version.
    latest = (row.edited_at or row.sent_at, bool(row.latest_is_edit), row.latest_update_id)
    incoming = (event_at, edited, update_id)
    if row.revision_count == 1 or incoming >= latest or row.source == "violation_excerpt":
        sender = message.get("sender_chat") or message.get("from") or {}
        row.sender_id = sender.get("id")
        row.sender_type = "chat" if message.get("sender_chat") else "bot" if sender.get("is_bot") else "user"
        row.sender_name = sender_name(sender)
        row.username = sender.get("username", "")
        row.message_type = next(
            (kind for kind in MEDIA if message.get(kind)), "text" if "text" in message else "service"
        )
        row.text, row.caption = message.get("text", ""), message.get("caption", "")
        row.sent_at, row.edited_at = message["date"], message.get("edit_date", 0)
        row.latest_update_id, row.latest_is_edit, row.source = update_id, edited, "telegram"
    await session.flush()
    delta = len(json.dumps(snapshot(message), ensure_ascii=True).encode("utf-8")) + 514 + visible_bytes(row) - before
    if counter:
        counter.value = str(used + delta)
    else:
        session.add(Meta(key=ARCHIVE_BYTES, value=str(used + delta)))
    increment(session, group_counter, group_used, group.id, delta)
    return row


async def mark_reasons(session, message, edited, update_id, reasons):
    row = await session.scalar(
        select(ArchivedMessage).where(
            ArchivedMessage.group_id == message["chat"]["id"],
            ArchivedMessage.message_id == message["message_id"],
        )
    )
    if row:
        revision = await session.scalar(
            select(MessageRevision).where(
                MessageRevision.archive_id == row.id,
                MessageRevision.version_key == version_key(message, edited, update_id),
            )
        )
        if revision:
            # Initialize the counter before changing data so an upgrade's first
            # adjustment does not count the same evidence twice.
            counter, used = await archive_bytes(session)
            group_counter, group_used = await group_bytes(session, row.group_id)
            before = len(json.dumps(revision.reasons).encode("utf-8"))
            revision.reasons = list(dict.fromkeys(revision.reasons + reasons))
            amount = len(json.dumps(revision.reasons).encode("utf-8")) - before
            if counter:
                counter.value = str(used + amount)
            else:
                session.add(Meta(key=ARCHIVE_BYTES, value=str(used + amount)))
            increment(session, group_counter, group_used, row.group_id, amount)


async def record_deletion(session, action):
    if action.kind != "delete" or action.payload.get("chat_id", action.group_id) != action.group_id:
        return
    row = await session.scalar(
        select(ArchivedMessage).where(
            ArchivedMessage.group_id == action.group_id,
            ArchivedMessage.message_id == action.payload.get("message_id"),
        )
    )
    if row is None:
        return
    # A later retry cannot erase an already confirmed deletion.
    if row.deletion_state in ("deleted", "already_absent") and action.state != "succeeded":
        return
    counter, used = await archive_bytes(session)
    group_counter, group_used = await group_bytes(session, row.group_id)
    before = visible_bytes(row)
    row.deletion_action_id, row.deletion_error = action.id, action.error
    row.deletion_state = (
        ("already_absent" if action.payload.get("already_absent") else "deleted")
        if action.state == "succeeded"
        else action.state
        if action.state in ("pending", "failed")
        else "not_requested"
    )
    if action.state == "succeeded":
        row.deleted_at = now()
    if counter:
        counter.value = str(used + visible_bytes(row) - before)
    else:
        session.add(Meta(key=ARCHIVE_BYTES, value=str(used + visible_bytes(row) - before)))
    increment(session, group_counter, group_used, row.group_id, visible_bytes(row) - before)


def conditions(group_id, filters: ArchiveFilter):
    result = [ArchivedMessage.group_id == group_id]
    if filters.sender_id is not None:
        result.append(ArchivedMessage.sender_id == filters.sender_id)
    if filters.after:
        result.append(ArchivedMessage.sent_at >= filters.after)
    if filters.before:
        result.append(ArchivedMessage.sent_at <= filters.before)
    if filters.message_type:
        result.append(ArchivedMessage.message_type == filters.message_type)
    if filters.deletion:
        result.append(ArchivedMessage.deletion_state == filters.deletion)
    if filters.flagged:
        result.append(
            select(Violation.id)
            .where(
                Violation.group_id == ArchivedMessage.group_id,
                Violation.message_id == ArchivedMessage.message_id,
            )
            .exists()
        )
    if filters.query.strip():
        # Search every observed revision, including the ad text later edited away.
        term = filters.query.strip()
        result.append(
            or_(
                ArchivedMessage.text.contains(term, autoescape=True),
                ArchivedMessage.caption.contains(term, autoescape=True),
                ArchivedMessage.sender_name.contains(term, autoescape=True),
                ArchivedMessage.username.contains(term, autoescape=True),
                select(MessageRevision.id)
                .where(
                    MessageRevision.archive_id == ArchivedMessage.id,
                    or_(
                        MessageRevision.payload["text"].as_string().contains(term, autoescape=True),
                        MessageRevision.payload["caption"].as_string().contains(term, autoescape=True),
                    ),
                )
                .exists(),
            )
        )
    return result


async def decorate(session, rows, detail=False):
    if not rows:
        return []
    ids = [row.id for row in rows]
    gaps = {marker.key.removeprefix("archive:gap:"): json.loads(marker.value)
            for marker in await session.scalars(select(Meta).where(
                Meta.key.in_([f"archive:gap:{identifier}" for identifier in ids])))}
    revisions = {}
    if detail:
        for revision in (
            await session.scalars(
                select(MessageRevision)
                .where(MessageRevision.archive_id.in_(ids))
                .order_by(MessageRevision.event_at, MessageRevision.update_id, MessageRevision.id)
            )
        ).all():
            revisions.setdefault(revision.archive_id, []).append(values(revision))
    group_id = rows[0].group_id
    violations = {
        row.message_id: values(row)
        for row in (
            await session.scalars(
                select(Violation).where(
                    Violation.group_id == group_id, Violation.message_id.in_([row.message_id for row in rows])
                )
            )
        ).all()
    }
    operations = []
    if detail:
        operations = (
            await session.scalars(
                select(Action)
                .where(
                    Action.group_id == group_id,
                    or_(
                        Action.id.in_([row.deletion_action_id for row in rows if row.deletion_action_id]),
                        Action.payload["violation_id"]
                        .as_string()
                        .in_([row["id"] for row in violations.values()]),
                    ),
                )
                .order_by(Action.created_at)
            )
        ).all()
    result = []
    for row in rows:
        data = values(row)
        from purrai.group_history import historical_source
        data.update(await historical_source(session, row.group_id, row.message_id))
        gap = gaps.get(row.id)
        if gap:
            data["recording_gap"] = {key: gap[key] for key in
                ("omitted_at_least", "first_skipped_at", "last_skipped_at", "reason")}
            data["recording_gap"]["latest_not_recorded"] = gap["order"] > [
                row.edited_at or row.sent_at, bool(row.latest_is_edit), row.latest_update_id]
        data["violation"] = violations.get(row.message_id)
        if detail:
            data["revisions"] = revisions.get(row.id, [])
            # Include actual action state and errors for subsequent ad analysis.
            data["actions"] = [
                values(a)
                for a in operations
                if a.id == row.deletion_action_id
                or data["violation"]
                and a.payload.get("violation_id") == data["violation"]["id"]
            ]
        result.append(data)
    return result


async def page(db, group_id, filters, page_number=1, size=20, *, settings=None):
    where = conditions(group_id, filters)
    async with db.sessions() as session:
        total = await session.scalar(select(func.count()).select_from(ArchivedMessage).where(*where))
        rows = (
            await session.scalars(
                select(ArchivedMessage)
                .where(*where)
                .order_by(ArchivedMessage.sent_at.desc(), ArchivedMessage.message_id.desc())
                .offset((page_number - 1) * size)
                .limit(size)
            )
        ).all()
        return {"items": await decorate(session, rows), "total": total, "page": page_number, "size": size,
                "recording": await recording_status(session, await session.get(Group, group_id), settings)}


async def detail(db, group_id, identifier):
    async with db.sessions() as session:
        row = await session.scalar(
            select(ArchivedMessage).where(
                ArchivedMessage.group_id == group_id, ArchivedMessage.id == identifier
            )
        )
        return (await decorate(session, [row], True))[0] if row else None


async def export_jsonl(db, group_id, filters, cutoff=None):
    """Keyset pagination bounds memory; one full message with all revisions per line."""
    cutoff = now() if cutoff is None else cutoff
    where = conditions(group_id, filters) + [ArchivedMessage.created_at <= cutoff]
    cursor = None
    while True:
        async with db.sessions() as session:
            query = select(ArchivedMessage).where(*where)
            if cursor:
                query = query.where(
                    or_(
                        ArchivedMessage.sent_at > cursor[0],
                        and_(ArchivedMessage.sent_at == cursor[0], ArchivedMessage.message_id > cursor[1]),
                    )
                )
            rows = (
                await session.scalars(
                    query.order_by(ArchivedMessage.sent_at, ArchivedMessage.message_id).limit(100)
                )
            ).all()
            data = await decorate(session, rows, True)
        if not rows:
            return
        for item in data:
            yield json.dumps({"schema_version": 1, **item}, ensure_ascii=False, separators=(",", ":")) + "\n"
        cursor = (rows[-1].sent_at, rows[-1].message_id)


async def export_document(db, group_id, filters, cutoff):
    result = await page(db, group_id, filters, size=1)
    if result["total"] > 1000:
        raise ValueError("Bot 每次最多导出 1000 条，请缩小时间范围或在网页导出全部匹配消息")
    content, count = bytearray(), 0
    async for line in export_jsonl(db, group_id, filters, cutoff):
        content.extend(line.encode("utf-8"))
        count += 1
        if count > 1000:
            raise ValueError("Bot 每次最多导出 1000 条，请缩小筛选范围或在网页导出")
        if len(content) > 20 * 1024 * 1024:
            raise ValueError("归档文件超过 20 MB，请缩小筛选范围或在网页导出")
    if not count:
        raise ValueError("筛选范围内暂无可导出的消息")
    return bytes(content), count
