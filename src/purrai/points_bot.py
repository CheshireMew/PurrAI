"""Legacy command aliases come from editable points policy, never ad-matching code."""
import html

from aiogram.exceptions import TelegramBadRequest
from sqlalchemy import func, select

from purrai.action_delivery import deliver_once
from purrai.action_queue import enqueue
from purrai.advertisement_store import latest_reviews
from purrai.advertisement_schemas import LOCAL_REVIEW_WINDOW_SECONDS
from purrai.chat_state import blocked_message
from purrai.errors import ManagementError
from purrai.command_text import normalized_command
from purrai.group_menu import matched_action
from purrai.models import Action, ArchivedMessage, Group, PointsAccount, PointsClaim, PointsPacket, now
from purrai.points_policy import get_policy
from purrai.points_rewards import claim_packet, create_packet, earn, user_account
from purrai.points_commands import adjust_from_reply, lookup_from_reply


def command(text, policy):
    words = normalized_command(text).split()
    if not words:
        return None, []
    aliases = [(kind, normalized_command(phrase))
               for kind, phrases in policy.commands.items() for phrase in phrases]
    for kind, phrase in aliases:
        if words[0] == phrase:
            if kind in ("packet", "add", "subtract", "lookup") or len(words) == 1:
                return kind, words[1:]
    # A complete configured alias wins over a prefix. For single-amount commands,
    # the longest alias makes digit-ending aliases unambiguous.
    if len(words) == 1:
        for kind, phrase in sorted(aliases, key=lambda item: len(item[1]), reverse=True):
            if kind not in ("add", "subtract") or not words[0].startswith(phrase):
                continue
            amount = words[0][len(phrase):]
            if amount.isascii() and amount.isdecimal():
                return kind, [amount]
    return None, []


async def resolved_command(session, group_id, text, policy):
    operation = await matched_action(session, group_id, text)
    if operation in ("signin", "balance"):
        return operation, []
    return command(text, policy)


async def ranking(session, group_id, page=1):
    query = select(PointsAccount).where(PointsAccount.group_id == group_id,
        PointsAccount.user_id.is_not(None), PointsAccount.balance > 0)
    total = await session.scalar(select(func.count()).select_from(query.subquery()))
    page = max(1, min(page, max(1, (total + 19) // 20)))
    rows = await session.scalars(query.order_by(PointsAccount.balance.desc(), PointsAccount.created_at, PointsAccount.id)
                                .offset((page - 1) * 20).limit(20))
    lines = ["积分榜"]
    for index, account in enumerate(rows, (page - 1) * 20 + 1):
        lines.append(f'{index}. <a href="tg://user?id={account.user_id}">{html.escape(account.display_name[:60])}</a>  {account.balance}')
    if not total:
        lines.append("还没有积分，先来签到吧。")
    buttons = []
    if page > 1:
        buttons.append({"text": "上一页", "callback_data": f"pts:rank:{page - 1}"})
    if page * 20 < total:
        buttons.append({"text": "下一页", "callback_data": f"pts:rank:{page + 1}"})
    return "\n".join(lines), {"inline_keyboard": [buttons] if buttons else []}


class PointsBot:
    def __init__(self, db, tg, answers=None):
        self.db, self.tg = db, tg
        from purrai.callback_answers import CallbackAnswers
        self.answers = answers or CallbackAnswers(tg, getattr(db, "settings", None))

    async def observe(self, message, *, edited=False):
        chat, user = message.get("chat", {}), message.get("from", {})
        if (chat.get("type") not in ("group", "supergroup") or not user.get("id") or user.get("is_bot")
                or message.get("sender_chat") or message.get("via_bot") or message.get("forward_origin")
                or not message.get("text") or edited or now() - message.get("date", 0) > 120):
            return False
        async with self.db.transaction() as session:
            group = await session.get(Group, chat["id"])
            if not group or not group.approved:
                return False
            policy = await get_policy(session, group.id)
            menu_operation = await matched_action(session, group.id, message["text"])
            if not policy.enabled and menu_operation not in ("signin", "balance"):
                return False
            kind, arguments = await resolved_command(session, group.id, message["text"], policy)
            if await session.scalar(select(blocked_message(group.id, message["message_id"], user["id"]))):
                return bool(kind)
            if not kind and (not policy.chat_points or message["text"].lstrip().startswith("/")):
                return False
            key = f"points:{group.id}:{message['message_id']}"
            if not await session.scalar(select(Action.id).where(Action.dedupe_key == key)):
                action = await enqueue(session, "points_message", key, group.id, user["id"],
                    {"message": message, "operation": kind or "chat", "arguments": arguments,
                     "message_id": message["message_id"], "expires_at": message["date"] + 120,
                     **({"menu_operation": menu_operation} if menu_operation in ("signin", "balance") else {})})
                if not kind:
                    action.available_at = now() + 5
            return bool(kind)

    async def callback(self, query):
        data = query.get("data", "")
        if not data.startswith("pts:"):
            return False
        message, user = query.get("message", {}), query["from"]
        group_id = message.get("chat", {}).get("id")
        answer = "此积分按钮已失效。"
        if not group_id or user.get("is_bot"):
            return True
        parts = data.split(":")
        async with self.db.sessions() as session:
            group = await session.get(Group, group_id)
            if not group or not group.approved:
                return True
            if len(parts) == 3 and parts[1] == "rank" and parts[2].isdecimal():
                if not (await get_policy(session, group_id)).enabled:
                    return True
                text, keyboard = await ranking(session, group_id, int(parts[2]))
                try:
                    await self.tg.call("edit_message_text", chat_id=group_id, message_id=message["message_id"],
                                       text=text, parse_mode="HTML", reply_markup=keyboard)
                except TelegramBadRequest as exc:
                    if "message is not modified" not in str(exc).lower():
                        raise
                await self.answers.send(query["id"])
                return True
            packet = await session.get(PointsPacket, parts[2]) if len(parts) == 3 and parts[1] == "claim" else None
            if packet and packet.group_id == group_id:
                previous = await session.get(PointsClaim, (packet.id, user["id"]))
                if previous:
                    answer = f"你已经领取过 {previous.points} 积分。"
                elif packet.expires_at <= now() or not packet.remaining_count or packet.target_id and packet.target_id != user["id"]:
                    answer = "红包已领完、已过期，或仅限指定群友领取。"
                else:
                    member = await self.tg.call("get_chat_member", chat_id=group_id, user_id=user["id"])
                    if member["status"] in ("creator", "administrator", "member") or member["status"] == "restricted" and member.get("is_member"):
                        async with self.db.transaction() as transaction:
                            packet = await transaction.get(PointsPacket, packet.id)
                            try:
                                answer = await claim_packet(transaction, packet, user)
                            except ManagementError as exc:
                                answer = str(exc)
                    else:
                        answer = "只有仍在本群的成员可以领取。"
        await self.answers.send(query["id"], answer, show_alert=True)
        return True

    async def close(self):
        await self.answers.close()


async def process_message(call, session, action, group):
    if not group or not group.approved:
        action.state = "cancelled"
        return
    if action.payload.get("source_expired") and "reply" not in action.payload:
        action.state, action.error = "cancelled", "原消息运行副本已到保留期限，不能重放旧积分指令"
        return
    message = action.payload.get("message") or action.payload.get("reply_source") or {}
    policy = await get_policy(session, group.id)
    if action.payload.get("menu_operation"):
        if await matched_action(session, group.id, message["text"]) != action.payload["menu_operation"]:
            action.state, action.error = "cancelled", "底部菜单已更新或清除，取消旧按钮任务"
            return
        if not policy.enabled:
            reply = "本群暂未开启积分功能，请联系群管理员。"
            action.payload = {**action.payload, "reply": reply, "keyboard": None}
    if "reply" not in action.payload:
        chat_reward = action.payload["operation"] == "chat"
        if (not policy.enabled or await session.scalar(select(blocked_message(group.id, message["message_id"], action.user_id)))):
            action.state = "cancelled"
            return
        review = (await latest_reviews(session, group.id, [message["message_id"]])).get(message["message_id"]) if chat_reward else None
        # A reward accepted at receipt can settle after a slow review. Commands
        # and messages without a review retain their original execution deadline.
        if not review and now() > action.payload["expires_at"]:
            action.state = "cancelled"
            return
        check = await session.scalar(select(Action).where(Action.dedupe_key == f"advertising-check:{group.id}:{message['message_id']}"))
        if check and check.state == "pending":
            action.available_at = now() + 2
            return False
        if check and check.state == "failed":
            action.state = "cancelled"
            return
        if review:
            if review.user_id != action.user_id or review.message.get("text") != message["text"]:
                action.state, action.error = "cancelled", "原消息已变化"
                return
            if review.state in ("pending", "requesting"):
                # The terminal verdict wakes this exact reward in the same transaction.
                # The deadline also recovers older jobs without a wakeup receipt.
                action.available_at = max(now() + 60, review.event_at + LOCAL_REVIEW_WINDOW_SECONDS)
                return False
            if review.state not in ("clean", "exempt", "ignored"):
                action.state, action.error = "cancelled", "此消息未通过广告审核"
                return
        source = await session.scalar(select(ArchivedMessage).where(
            ArchivedMessage.group_id == group.id, ArchivedMessage.message_id == message["message_id"]))
        if source and source.text != message["text"]:
            action.state = "cancelled"
            return
        kind, arguments = await resolved_command(session, group.id, message["text"], policy)
        if ((kind or "chat") != action.payload["operation"]
                or kind and arguments != action.payload["arguments"]):
            action.state = "cancelled"
            return
        if not kind and (not policy.chat_points or not policy.chat_daily_limit or
                sum(0x3400 <= ord(c) <= 0x9fff for c in message["text"]) < policy.chat_min_characters):
            return
        keyboard = None
        try:
            if kind in ("add", "subtract"):
                reply = await adjust_from_reply(call, session, action, group, policy, kind, arguments)
            elif kind == "lookup":
                reply = await lookup_from_reply(session, action, group, arguments)
            elif kind == "ranking":
                reply, keyboard = await ranking(session, group.id)
            elif kind == "balance":
                account = await user_account(session, group.id, message["from"])
                ahead = await session.scalar(select(func.count()).select_from(PointsAccount).where(
                    PointsAccount.group_id == group.id, PointsAccount.user_id.is_not(None), PointsAccount.balance > account.balance))
                reply = f"你当前有 {account.balance} 积分，排名第 {ahead + 1}。"
            elif kind == "packet":
                account = await user_account(session, group.id, message["from"])
                packet = await create_packet(session, account, policy, message, arguments)
                reply = f"积分红包 · {packet.total} 积分 / {packet.count} 份\n{'专属红包' if packet.target_id else '拼手气红包'}，{policy.packet_expiry_hours} 小时内可领取。"
                keyboard = {"inline_keyboard": [[{"text": "领取积分", "callback_data": f"pts:claim:{packet.id}"}]]}
            else:
                account = await user_account(session, group.id, message["from"])
                reply = await earn(session, account, policy, message, kind or "chat")
        except ManagementError as exc:
            reply = str(exc)
        action.payload = {**action.payload, "reply": reply, "keyboard": keyboard}
    reply = action.payload["reply"]
    if not reply:
        return
    options = {"message_thread_id": message["message_thread_id"]} if message.get("is_topic_message") else {}
    await deliver_once(call, session, action, "send_message", chat_id=group.id, text=reply,
        parse_mode="HTML" if action.payload["operation"] == "ranking" else None,
        reply_markup=action.payload.get("keyboard"), reply_parameters={"message_id": message["message_id"], "allow_sending_without_reply": True}, **options)
