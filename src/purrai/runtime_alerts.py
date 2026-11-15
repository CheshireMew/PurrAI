"""Persistent fault episodes and merged owner notifications through the action queue."""

import json
import logging
from collections import Counter

from sqlalchemy import or_, select

from purrai.action_queue import enqueue
from purrai.admin_labels import OPERATIONS, STATE
from purrai.models import Action, Group, Inbox, Meta, now
from purrai.runtime_notice_delivery import requeue_uncertain_notices

logger = logging.getLogger(__name__)


async def update_episode(session, key, message, owners, *, recovery="", heading="需要处理", details=None):
    marker = await session.get(Meta, "runtime-alert:" + key)
    state = json.loads(marker.value) if marker else {"episode": 0, "active": False}
    if bool(message) == state["active"]:
        if message and (state.get("message") != message or state.get("details") != (details or {})):
            state.update(message=message, details=details or {})
            marker.value = json.dumps(state, ensure_ascii=False)
        return False
    state.update(active=bool(message), changed_at=now(), message=message, details=details or {})
    if message:
        state["episode"] += 1
    log = logger.warning if message else logger.info
    log("运行状态变化 name=%s episode=%s active=%s detail=%s", key, state["episode"], bool(message),
        (message or recovery or "提醒已解除").replace("\n", "；"))
    for owner in owners:
        await enqueue(session, "owner_alert", f"owner-alert:{key}:{state['episode']}:{bool(message)}:{owner}",
            payload={"chat_id": owner, "text": ("PurrAI · " + heading + "\n" + message) if message else
                     ("PurrAI · 提醒已解除\n" + (recovery or key + "已恢复正常")),
                     "episode": state["episode"], "alert_key": key, "alert_active": bool(message),
                     "details": details or {}}, priority=25)
    if marker:
        marker.value = json.dumps(state, ensure_ascii=False)
    else:
        session.add(Meta(key="runtime-alert:" + key, value=json.dumps(state, ensure_ascii=False)))
    return True


def operation_summary(rows):
    labels = {"challenge": "发送入群验证码", "release": "解除验证禁言", "verify_mute": "入群验证前限制发言"}
    counts = Counter((row.title or "系统", labels.get(row.kind, OPERATIONS.get(row.kind, "其他操作"))) for row in rows)
    lines = [f"{title}：{kind} {count} 项" for (title, kind), count in counts.most_common(6)]
    if len(counts) > 6:
        lines.append(f"另有 {len(counts) - 6} 类操作，详见操作记录")
    return "\n".join(lines)


class RuntimeAlerts:
    def __init__(self, db, settings, runtime_status=None):
        self.db, self.settings, self.runtime_status = db, settings, runtime_status

    async def cycle(self):
        from purrai.archive import archive_bytes
        async with self.db.transaction() as session:
            owners = self.settings.superadmin_ids
            groups = (await session.scalars(select(Group).where(Group.approved.is_(True), Group.enabled.is_(True)))).all()
            bad = [group.title for group in groups if group.status == "error"]
            cutoff = now() - self.settings.alert_backlog_seconds
            # A recurring check keeps its first creation time. Only waiting past
            # this run's due time is a backlog; future cleanup/retries are not.
            columns = (Action.id, Action.kind, Group.title, Action.available_at)
            overdue_actions = (await session.execute(select(*columns).outerjoin(Group, Group.id == Action.group_id).where(
                Action.state == "pending", Action.available_at < cutoff,
                Action.kind.not_in(("owner_alert", "admin_notify", "progress")),
            ))).all()
            failures = (await session.execute(select(*columns).outerjoin(Group, Group.id == Action.group_id).where(
                Action.state == "failed", Action.kind.not_in(("owner_alert", "admin_notify")),
            ))).all()
            overdue_inbox = (await session.scalars(select(Inbox.id).where(
                Inbox.state == "pending", Inbox.available_at < cutoff,
            ))).all()
            failed_inbox = (await session.scalars(select(Inbox.id).where(Inbox.state == "failed"))).all()
            _, used = await archive_bytes(session)
            from purrai.archive_capacity import limits
            limit, _ = await limits(session, self.settings)
            await update_episode(session, "群保护权限", "以下群保护状态异常，请检查权限：" + "、".join(bad) if bad else "", owners)
            backlog = []
            if overdue_inbox:
                backlog.append(f"已收到的群消息或入群、验证事件中，有 {len(overdue_inbox)} 条尚未处理完。")
            if overdue_actions:
                backlog.append(f"有 {len(overdue_actions)} 项操作超过应执行时间仍未完成：\n" + operation_summary(overdue_actions))
            if backlog:
                backlog.append(f"以上均已超时至少 {self.settings.alert_backlog_seconds} 秒，机器人会继续处理。详情见系统状态。")
            await update_episode(session, "任务积压", "\n".join(backlog), owners, heading="处理延迟",
                recovery="目前没有超过等待时限的消息处理或群管理操作。",
                details={"action_ids": [row.id for row in overdue_actions], "event_ids": overdue_inbox})
            # Keep the episode's exact IDs so clearing the failed list can be
            # explained as success, cancellation or review, rather than recovery.
            marker = await session.get(Meta, "runtime-alert:失败操作")
            previous = json.loads(marker.value) if marker else {}
            previous_ids = previous.get("details", {}).get("action_ids", []) if previous.get("active") else []
            identifiers = sorted(set(previous_ids) | {row.id for row in failures})
            recovery = "目前没有待处理的失败操作。"
            if not failures and previous_ids:
                states = Counter()
                for offset in range(0, len(previous_ids), 400):
                    states.update(await session.scalars(select(Action.state).where(Action.id.in_(previous_ids[offset:offset + 400]))))
                missing = len(previous_ids) - sum(states.values())
                if missing:
                    states["记录已归档"] = missing
                recovery += "上次提醒的操作现为：" + "、".join(f"{STATE.get(state, state)} {count} 项" for state, count in states.items()) + "。"
            elif not failures and previous.get("active"):
                recovery += "旧提醒没有保存具体操作编号，不能据此认定全部重试成功。"
            await update_episode(session, "失败操作",
                f"有 {len(failures)} 项操作未完成，需要核实：\n{operation_summary(failures)}\n具体原因和重试入口：系统状态 → 失败操作。" if failures else "",
                owners, recovery=recovery, details={"action_ids": identifiers})
            await update_episode(session, "消息处理失败",
                f"有 {len(failed_inbox)} 条已收到的消息或入群、验证事件处理失败，已达到自动重试上限。请在系统状态 → 未处理事件查看具体原因并重试。" if failed_inbox else "",
                owners, recovery="目前没有达到重试上限而未处理的事件；具体处理结果以事件记录为准。",
                details={"event_ids": failed_inbox})
            await update_episode(session, "归档容量", f"归档已使用 {used // (1024 * 1024)} / {limit // (1024 * 1024)} MB，请导出并整理或调整额度"
                                 if used >= limit * 0.9 else "", owners)
            if self.runtime_status:
                status = self.runtime_status()
                health = status.get("telegram_health")
                if health is not None:
                    saved = await session.get(Meta, "telegram-health")
                    value = json.dumps(health, ensure_ascii=False)
                    if saved:
                        if saved.value != value:
                            saved.value = value
                    else:
                        session.add(Meta(key="telegram-health", value=value))
                error = status.get("poll_error", "")
                cause = "获取 Telegram 新消息的请求超时。" if "TimeoutError" in error else "暂时无法从 Telegram 获取新消息。"
                if not error and status.get("poll_stalled"):
                    error, cause = "stalled", "超过90秒没有完成一次 Telegram 新消息收取，可能已卡住。"
                followup = "期间新消息可能延迟处理，请查看系统状态。" if error == "stalled" else "机器人正在自动重连，期间新消息可能延迟处理。"
                await update_episode(session, "Telegram 收件", cause + followup if error else "",
                    owners, heading="连接暂时中断", recovery="已经重新连上 Telegram，恢复接收新消息。")
                problems = status.get("telegram_health", {}).get("problems", [])
                other = [item for item in problems if item["method"] not in ("get_me", "get_updates")]
                await update_episode(session, "Telegram 接口",
                    "以下 Telegram 操作尚未确认恢复：\n" + "\n".join(
                        f"{item['label']}：连续失败 {item['consecutive_failures']} 次，最近等待 {item['seconds']} 秒"
                        for item in other[:8]) + "\n能收到新消息不代表这些操作也已恢复。" if other else "", owners,
                    recovery="此前异常的 Telegram 操作已收到正常响应。",
                    details={"methods": [item["method"] for item in other]})
                faults = [item["name"] for item in status.get("workers", []) if item["state"] not in ("running", "stopped")]
                await update_episode(session, "后台任务", "后台任务正在恢复：" + "、".join(faults) if faults else "", owners)
            await requeue_uncertain_notices(session)
            notices = (await session.scalars(select(Action).where(Action.kind == "owner_alert", Action.state.in_(("pending", "failed")),
                or_(Action.state == "failed", Action.payload["delivery"]["state"].as_string().in_(("sending", "uncertain"))),
            ))).all()
            # A broken notification channel must be visible on the independent
            # web status, without recursively sending more Telegram alerts.
            await update_episode(session, "提醒发送",
                f"有 {len(notices)} 条运行提醒未完成发送。等待重试的会自动补发，已停止重试或被 Telegram 拒绝的请在失败操作中核实。" if notices else "",
                [], recovery="运行提醒已经确认发送，或已由更新的状态取代。",
                details={"action_ids": [item.id for item in notices]})

    async def status(self):
        async with self.db.sessions() as session:
            rows = (await session.scalars(select(Meta).where(Meta.key.startswith("runtime-alert:")))).all()
            return [{"name": row.key.removeprefix("runtime-alert:"), **json.loads(row.value)} for row in rows]
