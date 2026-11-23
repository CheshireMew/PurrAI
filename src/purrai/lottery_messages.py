"""Public rules and results use the exact saved configuration."""
from datetime import datetime

from sqlalchemy import select

from purrai.lottery import BEIJING, STATES
from purrai.models import LotteryEntry


def public_rules(row, remaining=None):
    config = row.config
    lines = [config["title"], STATES[row.state]]
    if config["description"]:
        lines.append(config["description"])
    cost = config["entry_cost"]
    lines.append(f"每次消耗 {cost} 积分" if config["mode"] == "instant" else (
        f"报名消耗 {cost} 积分，每人一次机会，未中奖不退" if cost else "免费报名，每人一次机会"))
    if config["min_balance"]:
        lines.append(f"参与前积分余额至少 {config['min_balance']} 分（门槛本身不扣分）")
    if config["min_active_days"]:
        lines.append(f"最近 {config['active_window_days']} 天，至少 {config['min_active_days']} 天获得聊天积分（含今天）")
    if config["draw_mode"] == "time":
        stamp = datetime.fromtimestamp(config["ends_at"], BEIJING).strftime("%Y-%m-%d %H:%M") if config["ends_at"] else "待设置结束时间"
        lines.append(f"{stamp}（北京时间）{'截止并自动开奖' if config['mode'] == 'raffle' else '结束'}")
    elif config["draw_mode"] == "participants":
        lines.append(f"满 {config['participant_target']} 人自动开奖")
    else:
        lines.append("管理员手动开奖" if config["mode"] == "raffle" else "管理员手动结束")
    lines.append(f"当前 {row.participant_count} 人参与" + (f"，已抽 {row.draw_count} 次" if config["mode"] == "instant" else ""))
    lines.append("奖品：")
    total_weight = sum(p["weight"] for p in config["prizes"])
    for index, prize in enumerate(config["prizes"]):
        label = prize["name"] + (f"（{prize['points']} 积分）" if prize["kind"] == "points" else "")
        count = f" · {prize['quantity']} 份" if prize["kind"] != "none" else ""
        if remaining is not None and prize["kind"] != "none":
            count = f" · 剩余 {remaining[index]}/{prize['quantity']} 份"
        probability = ""
        if config["mode"] == "instant":
            percent = f"{prize['weight'] / total_weight * 100:.6f}".rstrip("0").rstrip(".")
            probability = f" · 概率 {percent}%"
        lines.append(label + count + probability)
    if config["mode"] == "instant":
        lines.append(f"每人每天最多 {config['daily_limit']} 次，北京时间零点重置。每次独立随机；奖品领完后对应概率计为未中奖，其余概率不变。奖品全部领完后停止扣分。")
    else:
        lines.append(f"至少 {config['min_participants']} 人成团；不足则取消，退回报名积分。每人最多中奖一次；参与人数少于名额时，按上列顺序发放奖品。活动取消也会退款。")
    if any(p["kind"] == "manual" for p in config["prizes"]):
        lines.append("实物、兑换码等非积分奖品由群管理员发放。")
    return "\n".join(lines)


def keyboard(row):
    buttons = []
    if row.state == "open":
        label = f"消耗 {row.config['entry_cost']} 积分抽一次" if row.config["mode"] == "instant" else (
            f"消耗 {row.config['entry_cost']} 积分报名" if row.config["entry_cost"] else "免费参与抽奖")
        buttons.append([{"text": label, "callback_data": f"lot:join:{row.id}"}])
    buttons.append([{"text": "我的参与 / 中奖记录", "callback_data": f"lot:mine:{row.id}"}])
    return {"inline_keyboard": buttons}


async def public_result(session, row):
    lines = [row.config["title"], STATES[row.state], f"共 {row.participant_count} 人参与"]
    if row.state == "cancelled":
        lines.append("活动取消，报名积分已安排原路退回；未中奖不退的约定不适用于取消活动。")
        pending = await session.scalar(select(LotteryEntry.id).where(
            LotteryEntry.lottery_id == row.id, LotteryEntry.state == "refund_pending").limit(1))
        if pending:
            lines.append("个别账户余额已达上限，退款将保留并在余额降低后自动补发。")
    elif row.config["mode"] == "raffle":
        winners = await session.scalars(select(LotteryEntry).where(
            LotteryEntry.lottery_id == row.id, LotteryEntry.state == "won").order_by(LotteryEntry.prize_index, LotteryEntry.id))
        for entry in winners:
            lines.append(f"{entry.display_name[:30]}（{entry.user_id}）：{entry.prize_label}"
                + (f" · {entry.reward_points} 积分{'已入账' if entry.fulfilled else '待入账'}" if entry.reward_points else " · 待管理员发放"))
    else:
        lines.append(f"共完成 {row.draw_count} 次即时抽奖。已完成的抽奖保留结果，结束活动不退回已消耗积分。")
    return "\n".join(lines)
