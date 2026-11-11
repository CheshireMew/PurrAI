"""Storage accounting and editable global/per-group archive allowances."""

from sqlalchemy import text

from purrai.models import Meta


async def group_bytes(session, group_id):
    key = f"archive:group-bytes:{group_id}"
    marker = await session.get(Meta, key)
    if marker:
        return marker, int(marker.value)
    messages = await session.scalar(text(
        "SELECT coalesce(sum(length(cast(text AS BLOB))+length(cast(caption AS BLOB))+"
        "length(cast(sender_name AS BLOB))+length(cast(username AS BLOB))+length(cast(deletion_error AS BLOB))+512),0) "
        "FROM archived_messages WHERE group_id=:g"), {"g": group_id})
    revisions = await session.scalar(text(
        "SELECT coalesce(sum(length(cast(r.payload AS BLOB))+length(cast(r.reasons AS BLOB))+512),0) "
        "FROM message_revisions r JOIN archived_messages m ON m.id=r.archive_id WHERE m.group_id=:g"), {"g": group_id})
    return None, messages + revisions


async def limits(session, settings, group_id=None):
    global_row = await session.get(Meta, "archive:global-limit-mb")
    limit = int(global_row.value) if global_row else getattr(settings, "archive_max_db_mb", 512)
    group_row = await session.get(Meta, f"archive:quota:{group_id}") if group_id is not None else None
    return limit * 1024 * 1024, int(group_row.value) * 1024 * 1024 if group_row and group_row.value != "null" else None


def increment(session, marker, before, group_id, delta):
    if marker:
        marker.value = str(max(0, before + delta))
    else:
        session.add(Meta(key=f"archive:group-bytes:{group_id}", value=str(max(0, before + delta))))
