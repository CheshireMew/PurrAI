"""Notify once about unresolved faults; keep recovery and uncertain receipts quiet."""

import json
import logging

from aiogram.exceptions import TelegramNetworkError
from sqlalchemy import select

from purrai.action_delivery import deliver_once, uncertain_delivery
from purrai.models import Action, Meta, now

logger = logging.getLogger(__name__)


def identity(action):
    if "alert_key" in action.payload:
        return action.payload["alert_key"], action.payload["episode"], action.payload["alert_active"]
    # Read the old, internally generated queue key without interpreting its text.
    parts = action.dedupe_key.split(":")
    if len(parts) == 5 and parts[0] == "owner-alert" and parts[2].isdigit() and parts[3] in ("True", "False"):
        return parts[1], int(parts[2]), parts[3] == "True"
    return None


async def superseded(session, action):
    source = identity(action)
    if not source:
        return False
    if not source[2]:
        return True  # Recovery is recorded on the status page, never messaged.
    marker = await session.get(Meta, "runtime-alert:" + source[0])
    if not marker:
        return False
    state = json.loads(marker.value)
    return state["episode"] != source[1] or state["active"] != source[2]


async def reconcile_notices(session):
    rows = (await session.scalars(select(Action).where(Action.kind == "owner_alert",
        Action.state.in_(("pending", "failed")),
    ))).all()
    for action in rows:
        if await superseded(session, action):
            action.state, action.error, action.updated_at = "cancelled", "问题已恢复或提醒已过时，不再发送", now()


async def send_notice(call, session, action):
    if await superseded(session, action):
        action.state, action.error = "cancelled", "问题已恢复或提醒已过时，不再发送"
        return
    if uncertain_delivery(action.payload):
        action.state, action.error = "failed", "提醒是否送达无法确认，已记录到系统状态；为避免重复打扰，不再补发。"
        return False
    try:
        await deliver_once(call, session, action, "send_message", chat_id=action.payload["chat_id"], text=action.payload["text"][:4000])
    except ValueError as exc:
        if not uncertain_delivery(action.payload) or not isinstance(exc.__cause__, (TelegramNetworkError, TimeoutError, ConnectionError, OSError)):
            raise
        action.state = "failed"
        action.error = "提醒是否送达无法确认，已记录到系统状态；为避免重复打扰，不再补发。"
        logger.warning("运行提醒未确认，不再补发 action=%s attempt=%s", action.id, action.attempts)
        return False
