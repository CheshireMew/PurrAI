"""Transactional lottery participation, random selection and points settlement."""
import secrets
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from purrai.action_queue import enqueue
from purrai.errors import ManagementError
from purrai.lottery_schemas import LotteryConfig
from purrai.models import Group, Lottery, LotteryEntry, PointsAccount, PointsDaily, now, uid
from purrai.points_policy import get_policy
from purrai.points_rewards import credit, user_account
from purrai.points_schemas import MAX_POINTS
from purrai.sender_identity import sender_name

STATES = {"draft": "草稿", "open": "进行中", "drawn": "已开奖", "closed": "已结束", "cancelled": "已取消"}
ENTRY_STATES = {"joined": "已报名", "won": "已中奖", "lost": "未中奖", "refunded": "已退款", "refund_pending": "待退款"}
BEIJING = ZoneInfo("Asia/Shanghai")


def document(row):
    return {key: getattr(row, key) for key in ("id", "group_id", "config", "state", "version",
        "participant_count", "draw_count", "message_id", "created_by", "created_at", "opened_at", "finished_at")}


def entry_document(row):
    return {key: getattr(row, key) for key in ("id", "user_id", "display_name", "fee", "state",
        "prize_index", "prize_label", "reward_points", "fulfilled", "created_at")}


async def require_row(session, group_id, identifier):
    row = await session.get(Lottery, identifier)
    if row is None or row.group_id != group_id:
        raise ManagementError(404, "抽奖活动不存在")
    return row


async def notice(session, row, phase, key):
    await enqueue(session, "lottery_notice", f"lottery:{row.id}:{phase}:{key}", row.group_id,
                  payload={"lottery_id": row.id, "phase": phase, **({"entry_id": key} if phase == "winner" else {})})


async def points_available(session, group_id, config):
    needs_points = (config.entry_cost or config.min_balance or config.min_active_days
                    or any(p.kind == "points" for p in config.prizes))
    if needs_points and not (await get_policy(session, group_id)).enabled:
        raise ManagementError(409, "请先开启本群积分功能，再使用积分或活跃条件")


async def settle(session, entry):
    """Keep a pending receipt if an account temporarily has no balance headroom."""
    refund = entry.state == "refund_pending"
    amount = entry.fee if refund else entry.reward_points
    if not refund and (entry.state != "won" or not amount or entry.fulfilled):
        return
    account = await user_account(session, entry.group_id, {"id": entry.user_id, "first_name": entry.display_name})
    if account.balance + amount > MAX_POINTS:
        return
    await credit(session, account, amount, f"lottery-{'refund' if refund else 'prize'}:{entry.id}",
                 "lottery_refund" if refund else "lottery_prize",
                 "抽奖取消，退回报名积分" if refund else f"抽奖中奖：{entry.prize_label}")
    if refund:
        entry.state = "refunded"
    else:
        entry.fulfilled = True


async def award(session, entry, index, prize):
    entry.state, entry.prize_index, entry.prize_label = "won", index, prize.name
    entry.reward_points = prize.points
    await settle(session, entry)


async def cancel(session, row):
    config = LotteryConfig.model_validate(row.config)
    if row.state not in ("draft", "open") or config.mode == "instant" and row.state == "open":
        raise ManagementError(409, "此活动不能取消；即时抽奖发布后请使用结束活动")
    entries = await session.scalars(select(LotteryEntry).where(LotteryEntry.lottery_id == row.id))
    for entry in entries:
        entry.state = "refund_pending" if entry.fee else "refunded"
        if entry.fee:
            await settle(session, entry)
    was_open = row.state == "open"
    row.state, row.finished_at, row.version = "cancelled", now(), row.version + 1
    if was_open:
        await notice(session, row, "result", "cancel")


async def draw(session, row):
    config = LotteryConfig.model_validate(row.config)
    if row.state != "open" or config.mode != "raffle":
        raise ManagementError(409, "只有进行中的报名抽奖可以开奖")
    entries = (await session.scalars(select(LotteryEntry).where(
        LotteryEntry.lottery_id == row.id, LotteryEntry.state == "joined").order_by(LotteryEntry.id))).all()
    if len(entries) < config.min_participants:
        await cancel(session, row)
        return
    slots = [(index, prize) for index, prize in enumerate(config.prizes) for _ in range(prize.quantity)]
    selected = secrets.SystemRandom().sample(entries, min(len(entries), len(slots)))
    for entry in entries:
        entry.state = "lost"
    for entry, (index, prize) in zip(selected, slots):
        await award(session, entry, index, prize)
    row.state, row.finished_at, row.draw_count = "drawn", now(), len(entries)
    row.version += 1
    await notice(session, row, "result", "draw")


async def close(session, row):
    if row.state != "open" or row.config["mode"] != "instant":
        raise ManagementError(409, "只有进行中的即时抽奖可以结束")
    row.state, row.finished_at, row.version = "closed", now(), row.version + 1
    await notice(session, row, "result", "close")


async def expire(session, row):
    config = LotteryConfig.model_validate(row.config)
    if row.state == "open" and config.draw_mode == "time" and config.ends_at <= now():
        if config.mode == "raffle":
            await draw(session, row)
        else:
            await close(session, row)


async def participate(session, row, user, request_id):
    """Caller holds the database writer lock; no network I/O inside this transaction."""
    previous = await session.scalar(select(LotteryEntry).where(
        LotteryEntry.lottery_id == row.id, LotteryEntry.request_id == request_id))
    if previous:
        if previous.user_id != user["id"]:
            raise ManagementError(409, "参与请求与用户不一致")
        return previous
    config = LotteryConfig.model_validate(row.config)
    if config.mode == "raffle":
        previous = await session.scalar(select(LotteryEntry).where(
            LotteryEntry.lottery_id == row.id, LotteryEntry.user_id == user["id"]))
        if previous:
            return previous
    group = await session.get(Group, row.group_id)
    if not group or not group.approved or row.state != "open":
        raise ManagementError(409, "活动未开始或已结束")
    if config.draw_mode == "time" and config.ends_at <= now():
        raise ManagementError(409, "报名或抽奖时间已结束，正在结算")
    if user.get("is_bot") or not user.get("id"):
        raise ManagementError(403, "只有群成员本人可以参与")
    await points_available(session, row.group_id, config)
    day = datetime.fromtimestamp(now(), BEIJING).date()
    if config.min_active_days:
        since = (day - timedelta(days=config.active_window_days - 1)).isoformat()
        active = await session.scalar(select(func.count()).select_from(PointsDaily).where(
            PointsDaily.group_id == row.group_id, PointsDaily.user_id == user["id"],
            PointsDaily.day >= since, PointsDaily.day <= day.isoformat(), PointsDaily.chat_points > 0))
        if active < config.min_active_days:
            raise ManagementError(409, f"最近 {config.active_window_days} 天需有 {config.min_active_days} 天获得聊天积分，目前 {active} 天")
    account = None
    if config.entry_cost or config.min_balance:
        account = await session.scalar(select(PointsAccount).where(
            PointsAccount.group_id == row.group_id, PointsAccount.user_id == user["id"]))
        balance = account.balance if account else 0
        if balance < max(config.min_balance, config.entry_cost):
            raise ManagementError(409, f"当前 {balance} 积分；参与前至少需要 {max(config.min_balance, config.entry_cost)} 积分")
    existing = await session.scalar(select(func.count()).select_from(LotteryEntry).where(
        LotteryEntry.lottery_id == row.id, LotteryEntry.user_id == user["id"]))
    if config.mode == "instant":
        today = await session.scalar(select(func.count()).select_from(LotteryEntry).where(
            LotteryEntry.lottery_id == row.id, LotteryEntry.user_id == user["id"], LotteryEntry.day == day.isoformat()))
        if today >= config.daily_limit:
            raise ManagementError(409, f"今天已抽 {today} 次，达到每日 {config.daily_limit} 次上限")
        remaining = await remaining_prizes(session, row)
        if not any(left for index, left in enumerate(remaining) if config.prizes[index].kind != "none"):
            raise ManagementError(409, "奖品已全部抽完，本次未扣积分")
    entry = LotteryEntry(id=uid(), lottery_id=row.id, group_id=row.group_id, user_id=user["id"],
        display_name=sender_name(user), request_id=request_id, day=day.isoformat(), fee=config.entry_cost,
        state="joined", reward_points=0, fulfilled=False)
    session.add(entry)
    if config.entry_cost:
        await credit(session, account, -config.entry_cost, f"lottery-fee:{entry.id}", "lottery_fee",
                     f"抽奖{'报名' if config.mode == 'raffle' else '消费'}：{config.title}", user["id"])
    row.participant_count += int(not existing)
    if config.mode == "instant":
        value, index = secrets.randbelow(sum(p.weight for p in config.prizes)), 0
        for index, prize in enumerate(config.prizes):
            if value < prize.weight:
                break
            value -= prize.weight
        entry.state = "lost"
        if prize.kind != "none" and remaining[index] > 0:
            await award(session, entry, index, prize)
        row.draw_count += 1
        if entry.state == "won" and prize.kind == "manual":
            await notice(session, row, "winner", entry.id)
    await session.flush()
    if config.mode == "raffle" and config.draw_mode == "participants" and row.participant_count >= config.participant_target:
        await draw(session, row)
    return entry


async def remaining_prizes(session, row):
    counts = dict((await session.execute(select(LotteryEntry.prize_index, func.count()).where(
        LotteryEntry.lottery_id == row.id, LotteryEntry.state == "won").group_by(LotteryEntry.prize_index))).all())
    return [max(0, prize["quantity"] - counts.get(index, 0)) for index, prize in enumerate(row.config["prizes"])]


async def cycle(session):
    for row in await session.scalars(select(Lottery).where(Lottery.state == "open").order_by(Lottery.created_at)):
        await expire(session, row)
    entries = await session.scalars(select(LotteryEntry).where(
        (LotteryEntry.state == "refund_pending") | ((LotteryEntry.state == "won")
            & (LotteryEntry.reward_points > 0) & LotteryEntry.fulfilled.is_(False))).limit(100))
    for entry in entries:
        await settle(session, entry)


def outcome(entry):
    if entry.state == "won":
        return f"恭喜抽中：{entry.prize_label}。" + (
            f"奖励 {entry.reward_points} 积分{'已入账' if entry.fulfilled else '待入账，请先降低积分余额'}。"
            if entry.reward_points else "请联系群管理员领奖。")
    if entry.state == "lost":
        return "本次未中奖。" + (f"已消耗 {entry.fee} 积分。" if entry.fee else "")
    if entry.state in ("refunded", "refund_pending"):
        return "活动已取消。" + (f"报名积分 {entry.fee} {'已退回' if entry.state == 'refunded' else '待退回，请先降低积分余额'}。" if entry.fee else "")
    return f"报名成功，每人一次机会。{'已扣除 ' + str(entry.fee) + ' 积分，未中奖不退。' if entry.fee else '本次不扣积分。'}"
