"""Points earning and transfers; every balance change has a durable ledger entry."""
import hashlib
import secrets
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from purrai.errors import ManagementError
from purrai.models import PointsAccount, PointsClaim, PointsDaily, PointsEntry, PointsPacket, now, uid
from purrai.points import PointsLedger
from purrai.points_schemas import MAX_POINTS
from purrai.sender_identity import sender_name


async def user_account(session, group_id, user):
    account = await session.scalar(select(PointsAccount).where(
        PointsAccount.group_id == group_id, PointsAccount.user_id == user["id"]))
    if account is None:
        account = PointsAccount(group_id=group_id, source_key=f"telegram:{uid()}",
            user_id=user["id"], display_name=sender_name(user), balance=0)
        session.add(account)
        await session.flush()
    return account


async def credit(session, account, delta, key, kind, reason, actor=0):
    existing = await session.scalar(select(PointsEntry).where(
        PointsEntry.group_id == account.group_id, PointsEntry.request_id == key))
    if existing:
        return existing.result
    return await PointsLedger.apply_delta(session, account, delta, actor, key, kind, reason,
                                          {"account_id": account.id, "delta": delta})


async def daily_record(session, group_id, user_id, timestamp):
    day = datetime.fromtimestamp(timestamp, ZoneInfo("Asia/Shanghai")).date().isoformat()
    row = await session.get(PointsDaily, (group_id, user_id, day))
    if row is None:
        row = PointsDaily(group_id=group_id, user_id=user_id, day=day)
        session.add(row)
        await session.flush()
    return row


async def earn(session, account, policy, message, kind):
    timestamp = message["date"]
    daily = await daily_record(session, account.group_id, account.user_id, timestamp)
    key = f"{kind}:{account.user_id}:{daily.day}" if kind == "signin" else f"chat:{account.group_id}:{message['message_id']}"
    if kind == "signin":
        if daily.signed:
            return f"今天已经签到啦，当前 {account.balance} 积分。"
        await credit(session, account, policy.signin_points, key, "signin", "每日签到", account.user_id)
        daily.signed = True
        return f"签到成功，+{policy.signin_points} 积分！当前 {account.balance} 积分。"
    text = message.get("text") or ""
    digest = hashlib.sha256("".join(text.split()).casefold().encode()).hexdigest()
    if (sum(0x3400 <= ord(c) <= 0x9fff for c in text) < policy.chat_min_characters
            or not policy.chat_points or not policy.chat_daily_limit
            or timestamp - daily.last_chat_at < policy.chat_interval_seconds
            or digest == daily.last_digest or daily.chat_points >= policy.chat_daily_limit):
        return None
    if await session.scalar(select(PointsEntry.id).where(PointsEntry.group_id == account.group_id, PointsEntry.request_id == key)):
        return None
    amount = min(policy.chat_points, policy.chat_daily_limit - daily.chat_points)
    await credit(session, account, amount, key, "chat", "群内聊天", account.user_id)
    daily.chat_points += amount
    daily.last_chat_at, daily.last_digest = timestamp, digest
    return None


async def create_packet(session, account, policy, message, arguments):
    if not policy.packet_enabled:
        raise ManagementError(409, "本群暂未开启积分红包")
    if len(arguments) not in (1, 2) or any(not v.isdecimal() for v in arguments):
        raise ManagementError(422, "用法：积分红包 总积分 份数；回复群友发送可指定领取人。")
    total, count = int(arguments[0]), int(arguments[1]) if len(arguments) == 2 else 1
    target = (message.get("reply_to_message") or {}).get("from")
    if target and (target.get("is_bot") or target["id"] <= 0):
        raise ManagementError(422, "不能向机器人或匿名身份发送专属红包")
    if not 1 <= count <= 100 or not count <= total <= MAX_POINTS or target and count != 1:
        raise ManagementError(422, "红包为整数积分，每份至少 1 分，最多 100 份；专属红包只能有 1 份。")
    if account.balance < total:
        raise ManagementError(409, "积分不足，红包未发出。")
    packet = PointsPacket(group_id=account.group_id, sender_id=account.user_id,
        target_id=target["id"] if target else None, message_id=message["message_id"], total=total,
        count=count, remaining=total, remaining_count=count, expires_at=now() + policy.packet_expiry_hours * 3600)
    session.add(packet)
    await session.flush()
    await credit(session, account, -total, f"packet-send:{packet.id}", "packet_send", "发出积分红包", account.user_id)
    return packet


async def claim_packet(session, packet, user):
    from purrai.group_access import GroupAccess
    await GroupAccess.current_group(session, packet.group_id)
    previous = await session.get(PointsClaim, (packet.id, user["id"]))
    if previous:
        return f"你已经领取过 {previous.points} 积分。"
    if packet.refunded or packet.expires_at <= now():
        return "红包已过期，未领积分会退回发送者。"
    if packet.target_id and packet.target_id != user["id"]:
        return "这是发给指定群友的红包。"
    if not packet.remaining_count:
        return "红包已经领完啦。"
    account = await user_account(session, packet.group_id, user)
    upper = min(packet.remaining - packet.remaining_count + 1, max(1, 2 * packet.remaining // packet.remaining_count))
    amount = packet.remaining if packet.remaining_count == 1 else secrets.randbelow(upper) + 1
    await credit(session, account, amount, f"packet-claim:{packet.id}:{user['id']}", "packet_claim", "领取积分红包", user["id"])
    packet.remaining -= amount
    packet.remaining_count -= 1
    session.add(PointsClaim(packet_id=packet.id, user_id=user["id"], points=amount))
    return f"领取成功，+{amount} 积分！当前 {account.balance} 积分。"


async def refund_expired(session, limit=100):
    # Ineligible refunds remain durable, but cannot occupy every slot in a
    # bounded batch. Oldest eligible packets are settled before new arrivals.
    packets = (await session.scalars(select(PointsPacket).outerjoin(PointsAccount,
        (PointsAccount.group_id == PointsPacket.group_id) & (PointsAccount.user_id == PointsPacket.sender_id)
    ).where(PointsPacket.expires_at <= now(), PointsPacket.refunded.is_(False),
        (PointsPacket.remaining == 0) | ((PointsAccount.id.is_not(None))
            & (PointsAccount.balance <= MAX_POINTS - PointsPacket.remaining))
    ).order_by(PointsPacket.expires_at, PointsPacket.id).limit(limit))).all()
    for packet in packets:
        if packet.remaining:
            account = await session.scalar(select(PointsAccount).where(
                PointsAccount.group_id == packet.group_id, PointsAccount.user_id == packet.sender_id))
            # Earlier packets in this same transaction may have filled the
            # account. Never discard the remaining refund or overflow credit.
            if account is None or account.balance > MAX_POINTS - packet.remaining:
                continue
            await credit(session, account, packet.remaining, f"packet-refund:{packet.id}", "packet_refund", "红包过期，退回未领取积分")
        packet.remaining, packet.remaining_count, packet.refunded = 0, 0, True
    return len(packets)
