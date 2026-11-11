"""Cross-account spam evidence and expiring, reversible join-raid protection."""

import json

from sqlalchemy import func, select

from purrai.models import Fingerprint, Group, Member, Meta, Whitelist, now
from purrai.runtime_alerts import update_episode


async def emergency_state(session, group_id):
    marker = await session.get(Meta, f"emergency:{group_id}")
    data = json.loads(marker.value) if marker else {}
    if data.get("active") and data.get("until", 0) <= now():
        data.update(active=False, restored_at=now(), restored_by="自动到期")
        marker.value = json.dumps(data, ensure_ascii=False)
    return data


async def observe_join(session, group, event_date, recipients):
    if not group.settings.get("raid_enabled", False) or event_date < now() - group.settings.get("raid_window", 60):
        return
    data = await emergency_state(session, group.id)
    if data.get("active") or data.get("suppressed_until", 0) > now():
        return
    await session.flush()
    count = await session.scalar(select(func.count()).select_from(Member).where(
        Member.group_id == group.id, Member.join_observed.is_(True), Member.present.is_(True),
        Member.joined_at >= now() - group.settings.get("raid_window", 60)))
    if count < group.settings.get("raid_count", 10):
        return
    await activate(session, group, f"{group.settings.get('raid_window', 60)} 秒内观察到 {count} 个不同账号入群",
                   "自动检测", recipients)


async def activate(session, group, reason, actor, recipients):
    timestamp = now()
    data = {"active": True, "started_at": timestamp, "until": timestamp + group.settings.get("emergency_seconds", 1800),
            "reason": reason, "actor": actor}
    marker = await session.get(Meta, f"emergency:{group.id}")
    if marker:
        marker.value = json.dumps(data, ensure_ascii=False)
    else:
        session.add(Meta(key=f"emergency:{group.id}", value=json.dumps(data, ensure_ascii=False)))
    await update_episode(session, f"emergency:{group.id}",
                         f"{group.title}（{group.id}）启用临时应急保护：{reason}。新入群成员须验证；新人链接消息暂时拦截。到期恢复原配置。", recipients)
    return data


async def coordinated_reason(session, group, message, activity):
    if not group.settings.get("coordinated_enabled", False):
        return None
    sender = message.get("sender_chat") or message.get("from", {})
    row = await session.get(Fingerprint, (group.id, sender.get("id"), message["message_id"]))
    if not row or not row.digest or row.similarity_length < group.settings.get("coordinated_min_length", 12):
        return None
    # Replayed edits and delayed originals never create an extra account. The
    # digest contains normalized text; albums count by distinct sender, too.
    count = await session.scalar(select(func.count(func.distinct(Fingerprint.user_id))).where(
        Fingerprint.group_id == group.id, Fingerprint.digest == row.digest,
        ~select(Whitelist.user_id).where(Whitelist.group_id == group.id,
            Whitelist.user_id == Fingerprint.user_id, Whitelist.moderation.is_(True)).exists(),
        Fingerprint.created_at >= now() - group.settings.get("coordinated_window", 60)))
    if count >= group.settings.get("coordinated_count", 4):
        return f"多账号重复刷屏：{group.settings.get('coordinated_window', 60)} 秒内 {count} 个不同账号发送相同内容"
    return None


class CoordinatedProtection:
    def __init__(self, context):
        self.context = context

    async def status(self, actor, group_id):
        group = await self.context.access.authorize(actor, group_id)
        async with self.context.db.transaction() as session:
            data = await emergency_state(session, group_id)
        return {**data, "active": bool(data.get("active")), "raid_enabled": group.settings.get("raid_enabled", False),
                "base_verification_enabled": group.settings["verification_enabled"]}

    async def change(self, actor, group_id, body):
        await self.context.access.authorize(actor, group_id, write=True)
        async with self.context.access.group_transaction(group_id) as session:
            intent = body.model_dump(exclude={"request_id"})
            previous = await self.context.previous(session, actor, body.request_id, intent)
            if previous is not None:
                return previous
            group = await session.get(Group, group_id)
            if body.active:
                data = await activate(session, group, body.reason, actor, self.context.settings.superadmin_ids)
            else:
                data = await emergency_state(session, group_id)
                data.update(active=False, restored_at=now(), restored_by=actor, restore_reason=body.reason,
                            suppressed_until=now() + group.settings.get("raid_window", 60))
                marker = await session.get(Meta, f"emergency:{group_id}")
                if marker:
                    marker.value = json.dumps(data, ensure_ascii=False)
                await update_episode(session, f"emergency:{group_id}", "", self.context.settings.superadmin_ids)
            return await self.context.record(session, actor, group_id, "emergency", body.reason, data,
                                             body.request_id, intent)

    async def cycle(self):
        async with self.context.db.transaction() as session:
            for group in await session.scalars(select(Group).where(Group.approved.is_(True))):
                data = await emergency_state(session, group.id)
                if data and not data.get("active"):
                    await update_episode(session, f"emergency:{group.id}", "", self.context.settings.superadmin_ids)
