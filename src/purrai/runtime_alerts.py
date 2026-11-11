"""Persistent fault episodes and merged owner notifications through the action queue."""

import json

from sqlalchemy import func, select

from purrai.action_queue import enqueue
from purrai.models import Action, Group, Inbox, Meta, now


async def update_episode(session, key, message, owners):
    marker = await session.get(Meta, "runtime-alert:" + key)
    state = json.loads(marker.value) if marker else {"episode": 0, "active": False}
    if bool(message) == state["active"]:
        return False
    state.update(active=bool(message), changed_at=now(), message=message)
    if message:
        state["episode"] += 1
    for owner in owners:
        await enqueue(session, "owner_alert", f"owner-alert:{key}:{state['episode']}:{bool(message)}:{owner}",
            payload={"chat_id": owner, "text": ("PurrAI · 需要处理\n" + message) if message else
                     ("PurrAI · 已恢复\n" + key), "episode": state["episode"]}, priority=25)
    if marker:
        marker.value = json.dumps(state, ensure_ascii=False)
    else:
        session.add(Meta(key="runtime-alert:" + key, value=json.dumps(state, ensure_ascii=False)))
    return True


class RuntimeAlerts:
    def __init__(self, db, settings, runtime_status=None):
        self.db, self.settings, self.runtime_status = db, settings, runtime_status

    async def cycle(self):
        from purrai.archive import archive_bytes
        async with self.db.transaction() as session:
            owners = self.settings.superadmin_ids
            groups = (await session.scalars(select(Group).where(Group.approved.is_(True), Group.enabled.is_(True)))).all()
            bad = [group.title for group in groups if group.status == "error"]
            oldest = await session.scalar(select(func.min(Action.created_at)).where(Action.state == "pending",
                Action.kind.not_in(("owner_alert", "admin_notify", "progress", "delete"))))
            failures = await session.scalar(select(func.count()).select_from(Action).where(Action.state == "failed",
                Action.kind.not_in(("owner_alert", "admin_notify"))))
            inbox = await session.scalar(select(func.min(Inbox.available_at)).where(Inbox.state == "pending"))
            _, used = await archive_bytes(session)
            from purrai.archive_capacity import limits
            limit, _ = await limits(session, self.settings)
            await update_episode(session, "群保护权限", "以下群保护状态异常，请检查权限：" + "、".join(bad) if bad else "", owners)
            overdue = (oldest is not None and oldest < now() - self.settings.alert_backlog_seconds) or (
                inbox is not None and inbox < now() - self.settings.alert_backlog_seconds)
            await update_episode(session, "任务积压", "存在长时间未推进的收件或处置任务，请查看系统状态和操作记录" if overdue else "", owners)
            await update_episode(session, "失败操作", f"存在 {failures} 项失败操作，请在系统状态核实或重试" if failures else "", owners)
            await update_episode(session, "归档容量", f"归档已使用 {used // (1024 * 1024)} / {limit // (1024 * 1024)} MB，请导出并整理或调整额度"
                                 if used >= limit * 0.9 else "", owners)
            if self.runtime_status:
                status = self.runtime_status()
                await update_episode(session, "Telegram 收件", status.get("poll_error", ""), owners)
                faults = [item["name"] for item in status.get("workers", []) if item["state"] not in ("running", "stopped")]
                await update_episode(session, "后台任务", "后台任务正在恢复：" + "、".join(faults) if faults else "", owners)

    async def status(self):
        async with self.db.sessions() as session:
            rows = (await session.scalars(select(Meta).where(Meta.key.startswith("runtime-alert:")))).all()
            return [{"name": row.key.removeprefix("runtime-alert:"), **json.loads(row.value)} for row in rows]
