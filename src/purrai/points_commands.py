"""Reply-based point operations. Command words live in the saved group policy."""
from sqlalchemy import func, select

from purrai.errors import ManagementError
from purrai.models import PointsAccount, now
from purrai.points_policy import get_policy
from purrai.points_rewards import credit, user_account
from purrai.points_schemas import MAX_POINTS
from purrai.sender_identity import sender_name
from purrai.telegram import is_admin


def reply_user(message, *, required):
    replied = message.get("reply_to_message")
    if not replied:
        if required:
            raise ManagementError(422, "请回复要操作的群友消息，再发送口令。")
        if message.get("external_reply"):
            raise ManagementError(422, "请回复本群成员的消息。")
        return message["from"]
    target = replied.get("from") or {}
    if (replied.get("sender_chat") or target.get("is_bot") or target.get("id", 0) <= 0
            or replied.get("chat", {}).get("id", message["chat"]["id"]) != message["chat"]["id"]):
        raise ManagementError(422, "请回复本群成员的消息，机器人、频道和匿名身份不支持此操作。")
    return target


async def require_admin(call, session, action, group, policy):
    member = await call(session, action, "get_chat_member", chat_id=group.id, user_id=action.user_id)
    if not is_admin(member):
        raise ManagementError(403, "只有本群管理员可以执行此操作。")
    # The transport releases the DB lock during I/O. Recheck settings before crediting.
    current = await get_policy(session, group.id)
    if not group.approved or current != policy or now() > action.payload["expires_at"]:
        raise ManagementError(409, "积分设置已变化或指令已过期，请重新发送。")


async def adjust_from_reply(call, session, action, group, policy, kind, arguments):
    target = reply_user(action.payload["message"], required=True)
    if (len(arguments) != 1 or not arguments[0].isascii() or not arguments[0].isdecimal()
            or len(arguments[0]) > 16 or not 1 <= int(arguments[0]) <= MAX_POINTS):
        raise ManagementError(422, "请在口令后填写正整数，空格可省略，例如：口令 100 或口令100。")
    await require_admin(call, session, action, group, policy)
    amount = int(arguments[0]) * (1 if kind == "add" else -1)
    account = await session.scalar(select(PointsAccount).where(
        PointsAccount.group_id == group.id, PointsAccount.user_id == target["id"]))
    if account is None and amount < 0:
        raise ManagementError(409, "积分不足，尚无可扣除的积分。")
    if account is None:
        account = await user_account(session, group.id, target)
    message = action.payload["message"]
    result = await credit(session, account, amount, f"admin-adjust:{group.id}:{message['message_id']}", "adjustment",
        f"管理员回复调整积分；指令消息 {message['message_id']}；目标消息 {message['reply_to_message']['message_id']}",
        action.user_id)
    return f"{sender_name(target)}：{amount:+d} 积分，当前 {result['balance']} 积分。"


async def lookup_from_reply(session, action, group, arguments):
    if arguments:
        raise ManagementError(422, "查询口令不需要数字；回复群友消息可查询对方，不回复则查询自己。")
    message = action.payload["message"]
    target = reply_user(message, required=False)
    account = await session.scalar(select(PointsAccount).where(
        PointsAccount.group_id == group.id, PointsAccount.user_id == target["id"]))
    if account is None:
        return f"{sender_name(target)} 当前有 0 积分，暂无积分记录。"
    ahead = await session.scalar(select(func.count()).select_from(PointsAccount).where(
        PointsAccount.group_id == group.id, PointsAccount.user_id.is_not(None), PointsAccount.balance > account.balance))
    return f"{sender_name(target)} 当前有 {account.balance} 积分，排名第 {ahead + 1}。"
