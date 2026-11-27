"""Group buttons and durable announcements; no AI is involved in drawing."""
from sqlalchemy import select

from purrai.action_delivery import deliver_once
from purrai.action_queue import enqueue
from purrai.errors import ManagementError
from purrai.group_menu import matched_action
from purrai.lottery import ENTRY_STATES, outcome, participate, remaining_prizes, require_row
from purrai.lottery_messages import keyboard, public_result, public_rules
from purrai.models import Group, Lottery, LotteryEntry, now


class LotteryBot:
    def __init__(self, db, tg, answers=None):
        self.db, self.tg = db, tg
        from purrai.callback_answers import CallbackAnswers
        self.answers = answers or CallbackAnswers(tg, getattr(db, "settings", None))

    async def observe(self, message):
        words = message.get("text", "").strip().split()
        chat, user = message.get("chat", {}), message.get("from", {})
        if (chat.get("type") not in ("group", "supergroup") or not user.get("id") or user.get("is_bot")
                or message.get("sender_chat") or now() - message.get("date", 0) > 120):
            return False
        async with self.db.transaction() as session:
            group = await session.get(Group, chat["id"])
            if not group or not group.approved:
                return False
            menu_operation = await matched_action(session, group.id, message.get("text", ""))
            if menu_operation != "lottery":
                if menu_operation or len(words) != 1 or words[0].split("@", 1)[0].lower() != "/lottery":
                    return False
                if "@" in words[0] and words[0].split("@", 1)[1].casefold() != getattr(self.tg, "username", "").casefold():
                    return False
            elif message.get("via_bot") or message.get("forward_origin"):
                return False
            await enqueue(session, "lottery_notice", f"lottery-list:{group.id}:{message['message_id']}", group.id,
                payload={"phase": "list", **({"menu_text": message["text"]} if menu_operation else {})})
        return True

    async def callback(self, query):
        data = query.get("data", "")
        if not data.startswith("lot:"):
            return False
        parts = data.split(":")
        group_id = query.get("message", {}).get("chat", {}).get("id")
        user, answer = query.get("from", {}), "此抽奖按钮已失效。"
        try:
            if len(parts) != 3 or parts[1] not in ("join", "mine", "rules") or not group_id or not user.get("id") or user.get("is_bot"):
                raise ManagementError(403, answer)
            async with self.db.sessions() as session:
                group = await session.get(Group, group_id)
                if not group or not group.approved:
                    raise ManagementError(403, "本群未获准使用机器人")
                await require_row(session, group_id, parts[2])
            if parts[1] == "rules":
                async with self.db.transaction() as session:
                    await enqueue(session, "lottery_notice", f"lottery-rules:{query['id']}", group_id,
                        payload={"lottery_id": parts[2], "phase": "rules"})
                answer = "完整规则已安排发送到群内，请核对后参与。"
            elif parts[1] == "join":
                member = await self.tg.call("get_chat_member", chat_id=group_id, user_id=user["id"])
                if not (member["status"] in ("member", "creator", "administrator")
                        or member["status"] == "restricted" and member.get("is_member")):
                    raise ManagementError(403, "只有仍在本群的成员可以参与")
                async with self.db.transaction() as session:
                    row = await require_row(session, group_id, parts[2])
                    entry = await participate(session, row, user, query["id"])
                    answer = outcome(entry)
            else:
                async with self.db.sessions() as session:
                    entries = (await session.scalars(select(LotteryEntry).where(
                        LotteryEntry.lottery_id == parts[2], LotteryEntry.user_id == user["id"])
                        .order_by(LotteryEntry.created_at.desc(), LotteryEntry.id).limit(3))).all()
                    answer = "最近的参与记录：\n" + "\n".join(
                        f"{ENTRY_STATES[entry.state]}" + (f"：{entry.prize_label[:32]}" if entry.prize_label else "")
                        + ("（已发放）" if entry.state == "won" and entry.fulfilled else "") for entry in entries) if entries else "你还没有参加此活动。"
        except ManagementError as exc:
            answer = str(exc)
        await self.answers.send(query["id"], answer, show_alert=True)
        return True

    async def close(self):
        await self.answers.close()


async def process_notice(call, session, action, group):
    if not group or not group.approved:
        action.state = "cancelled"
        return
    phase = action.payload["phase"]
    if phase == "list":
        if ("menu_text" in action.payload
                and await matched_action(session, group.id, action.payload["menu_text"]) != "lottery"):
            action.state, action.error = "cancelled", "底部菜单已更新或清除，取消旧按钮任务"
            return
        rows = (await session.scalars(select(Lottery).where(Lottery.group_id == group.id,
            Lottery.state == "open").order_by(Lottery.created_at.desc()).limit(10))).all()
        text = "本群进行中的抽奖\n点击活动查看完整规则后再参与。" if rows else "本群暂时没有进行中的抽奖。"
        markup = {"inline_keyboard": [[{"text": row.config["title"], "callback_data": f"lot:rules:{row.id}"}] for row in rows]}
        # A rule view is deliberately separate from a paid participation button.
        await deliver_once(call, session, action, "send_message", chat_id=group.id, text=text, reply_markup=markup)
        return
    row = await require_row(session, group.id, action.payload["lottery_id"])
    if phase == "winner":
        entry = await session.get(LotteryEntry, action.payload["entry_id"])
        text = f"{row.config['title']}\n{entry.display_name[:60]}（{entry.user_id}）抽中：{entry.prize_label}\n请联系群管理员领奖。"
        markup = None
    elif phase in ("announcement", "rules") and row.state == "open":
        text, markup = public_rules(row, await remaining_prizes(session, row)), keyboard(row)
    else:
        text, markup = await public_result(session, row), keyboard(row)
    response = await deliver_once(call, session, action, "send_message", chat_id=group.id, text=text, reply_markup=markup)
    if phase == "announcement" and response.get("message_id"):
        row.message_id = response["message_id"]
