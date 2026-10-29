"""Private-group admission, shared by ingestion and execution boundaries."""

from sqlalchemy import func, select

from purrai.shared_policy import initial_settings
from purrai.models import Group


async def approved(session, group_id):
    group = await session.get(Group, group_id)
    return bool(group and group.approved)


async def discover(session, chat, pending_limit=50):
    if chat.get("type") not in ("group", "supergroup"):
        return None
    group = await session.get(Group, chat["id"])
    if group is not None:
        return group
    pending = await session.scalar(select(func.count()).select_from(Group).where(Group.approved.is_(False)))
    if pending >= pending_limit:
        return None
    group = Group(id=chat["id"], title=chat.get("title", "")[:128], type=chat["type"],
                  enabled=False, approved=False, status="unapproved", settings=await initial_settings(session))
    session.add(group)
    await session.flush()
    return group


async def update_group_id(session, payload):
    for field in ("message", "edited_message", "chat_member", "my_chat_member", "chat_join_request"):
        chat = payload.get(field, {}).get("chat", {})
        if chat.get("type") in ("group", "supergroup"):
            return chat["id"]
    data = payload.get("callback_query", {}).get("data", "").split(":")
    if len(data) == 4 and data[0] == "v":
        from purrai.models import Verification

        verification = await session.get(Verification, data[1])
        if verification:
            return verification.group_id
