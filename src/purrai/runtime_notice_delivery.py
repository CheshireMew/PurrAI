"""Bounded, explicitly duplicate-tolerant delivery of operational notices only."""

import json
import logging

from aiogram.exceptions import TelegramNetworkError
from sqlalchemy import select

from purrai.action_delivery import deliver_once, uncertain_delivery
from purrai.models import Action, Meta, now

logger = logging.getLogger(__name__)
MAX_ATTEMPTS = 8


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
    marker = await session.get(Meta, "runtime-alert:" + source[0])
    if not marker:
        return False
    state = json.loads(marker.value)
    return state["episode"] != source[1] or state["active"] != source[2]


async def requeue_uncertain_notices(session):
    # This never retries a CAPTCHA, moderation effect, ordinary reply or invite.
    rows = (await session.scalars(select(Action).where(Action.kind == "owner_alert", Action.state == "failed",
        Action.payload["delivery"]["state"].as_string().in_(("sending", "uncertain")),
    ))).all()
    for action in rows:
        if await superseded(session, action):
            action.state, action.error, action.updated_at = "cancelled", "已有更新的运行状态，不再补发过时提醒", now()
        elif action.attempts < MAX_ATTEMPTS:
            action.state = "pending"
            action.available_at = max(now(), action.updated_at + min(300, 30 * 2 ** max(0, action.attempts - 1)))
            logger.info("运行提醒恢复重试 action=%s attempt=%s", action.id, action.attempts)


async def send_notice(call, session, action):
    if await superseded(session, action):
        action.state, action.error = "cancelled", "已有更新的运行状态，不再补发过时提醒"
        return
    ambiguous = uncertain_delivery(action.payload)
    if ambiguous and action.attempts > MAX_ATTEMPTS:
        action.state, action.error = "failed", "运行提醒已达到补发上限，请在系统状态核实发送结果。"
        return False
    if ambiguous:
        history = [*action.payload.get("notice_delivery_history", []), action.payload["delivery"]][-MAX_ATTEMPTS:]
        action.payload = {**action.payload, "notice_delivery_history": history, "delivery": {"state": "rejected"}}
    text = ("补发运行提醒：前次发送未获确认，可能重复。\n" if ambiguous or action.payload.get("notice_delivery_history") else "") + action.payload["text"]
    try:
        await deliver_once(call, session, action, "send_message", chat_id=action.payload["chat_id"], text=text[:4000])
    except ValueError as exc:
        if not uncertain_delivery(action.payload) or not isinstance(exc.__cause__, (TelegramNetworkError, TimeoutError, ConnectionError, OSError)):
            raise
        delay = min(300, 30 * 2 ** max(0, action.attempts - 1))
        action.state = "failed" if action.attempts >= MAX_ATTEMPTS else "pending"
        action.available_at = now() + delay
        action.error = ("运行提醒多次发送未获确认，已停止自动补发，请查看系统状态。" if action.state == "failed" else
                        f"运行提醒发送未获确认，将在{delay}秒后补发；可能收到重复提醒。")
        logger.warning("运行提醒未确认 action=%s attempt=%s state=%s retry_seconds=%s",
                       action.id, action.attempts, action.state, delay)
        return False
