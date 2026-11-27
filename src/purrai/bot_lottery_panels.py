"""Private Telegram lottery drafts, reuse, publication and prize management."""
from datetime import datetime
from types import SimpleNamespace

from purrai.bot_common import button
from purrai.errors import ManagementError
from purrai.lottery import BEIJING, STATES
from purrai.lottery_messages import public_rules
from purrai.lottery_schemas import LotteryConfig, LotteryOperation, LotterySave
from purrai.models import now


class LotteryPanels:
    def __init__(self, management, ui, state):
        self.management, self.admin = management, management.lotteries
        self.panel, self.send, self.remember = ui.panel, ui.send, state.remember

    async def home(self, actor, group_id, page=0, message_id=None):
        result = await self.admin.page(actor, group_id, page + 1, 5)
        buttons = [button(f"{STATES[row['state']]} · {row['config']['title'][:32]}", "lottery_detail", id=row["id"])
                   for row in result["items"]]
        buttons += [button("新建免费报名抽奖", "lottery_new", mode="raffle", cost=0),
                    button("新建积分报名抽奖", "lottery_new", mode="raffle", cost=100),
                    button("新建积分即时抽奖", "lottery_new", mode="instant", cost=50)]
        if page:
            buttons.append(button("上一页", "lottery_home", page=page - 1))
        if result["total"] > (page + 1) * 5:
            buttons.append(button("下一页", "lottery_home", page=page + 1))
        buttons.append(button("返回群管理", "group", id=group_id))
        await self.panel(actor, f"抽奖管理 · 共 {result['total']} 场\n选择往期活动可复用配置。群友可发送 /lottery 查看进行中的活动。",
            buttons, group_id, message_id, resume={"action": "lottery_home", "data": {"page": page}})

    async def detail(self, actor, group_id, identifier, message_id=None, page=0):
        row = await self.admin.get(actor, group_id, identifier, page + 1, "won", size=5)
        data = {"id": identifier, "version": row["version"]}
        buttons = []
        if row["state"] == "draft":
            fields = [("title", "修改标题"), ("description", "修改说明"), ("prizes", "设置奖品"),
                ("entry_cost", "设置消耗积分"), ("min_balance", "设置积分门槛"),
                ("activity", "设置活跃条件"), ("schedule", "设置开奖 / 结束方式")]
            fields += [("daily_limit", "每日抽奖次数")] if row["config"]["mode"] == "instant" else [("min_participants", "最低成团人数")]
            buttons += [button(label, "lottery_field", field=field, **data) for field, label in fields]
            buttons.append(button("发布到本群", "lottery_confirm", operation="publish", **data))
        if row["state"] == "open":
            operation = "draw" if row["config"]["mode"] == "raffle" else "close"
            buttons.append(button("立即开奖" if operation == "draw" else "结束即时抽奖", "lottery_confirm", operation=operation, **data))
        if row["state"] == "draft" or row["state"] == "open" and row["config"]["mode"] == "raffle":
            buttons.append(button("取消活动并退报名积分", "lottery_confirm", operation="cancel", **data))
        if row["state"] != "draft":
            buttons.append(button("重新发送公告 / 结果", "lottery_confirm", operation="announce", **data))
        buttons.append(button("复用配置，新建一场", "lottery_execute", operation="copy", **data))
        text = public_rules(SimpleNamespace(**row), row["remaining"])
        if row["entries"]:
            text += "\n\n中奖记录："
            for entry in row["entries"]:
                text += f"\n{entry['display_name'][:30]}（{entry['user_id']}）：{entry['prize_label']} · {'已发放' if entry['fulfilled'] else '待发放'}"
                if not entry["fulfilled"] and not entry["reward_points"]:
                    buttons.append(button(f"登记已发：{entry['display_name'][:15]} · {entry['prize_label'][:15]}",
                        "lottery_confirm", operation="fulfill", entry_id=entry["id"], **data))
        if page:
            buttons.append(button("上一页中奖记录", "lottery_detail", id=identifier, page=page - 1))
        if row["total_entries"] > (page + 1) * 5:
            buttons.append(button("下一页中奖记录", "lottery_detail", id=identifier, page=page + 1))
        for delivery in row["deliveries"][:1]:
            text += f"\n最近公告：{delivery['state']}" + (f" · {delivery['error'][:200]}" if delivery["error"] else "")
        buttons.append(button("返回抽奖管理", "lottery_home"))
        await self.panel(actor, text, buttons, group_id, message_id,
                         resume={"action": "lottery_detail", "data": {"id": identifier, "page": page}})

    async def prompt(self, actor, group_id, data, text):
        await self.management.authorize(actor, group_id, write=True)
        await self.remember(f"bot_prompt:{actor}", {"group_id": group_id,
            "data": {"kind": "lottery", **data}, "expires": now() + 900})
        await self.send(actor, text + "\n发送 /cancel 取消；15 分钟内有效。")

    async def route(self, actor, group_id, action, data, request_id, message_id):
        await self.remember(f"bot_prompt:{actor}", None)
        if action == "lottery_home":
            return await self.home(actor, group_id, data.get("page", 0), message_id)
        if action == "lottery_detail":
            return await self.detail(actor, group_id, data["id"], message_id, data.get("page", 0))
        if action == "lottery_new":
            return await self.prompt(actor, group_id, {"field": "new_title", **data}, "请发送抽奖标题，最多 80 个字符。创建后可继续设置奖品和参与条件。")
        row = await self.admin.get(actor, group_id, data["id"])
        if action == "lottery_execute":
            result = await self.admin.operate(actor, group_id, row["id"], LotteryOperation(
                operation=data["operation"], version=data["version"], entry_id=data.get("entry_id", "")), request_id)
            await self.send(actor, "已复用为新草稿，请核对配置并重新设置定时时间。" if data["operation"] == "copy" else "已处理；公告发送情况可在活动详情查看。")
            return await self.detail(actor, group_id, result["id"], message_id)
        if action == "lottery_confirm":
            notes = {"publish": "确认按当前规则发布到本群？发布后不可修改规则。",
                "draw": "确认立即开奖？不足最低成团人数时将取消并退款。",
                "cancel": "确认取消活动？已收取的报名积分将原路退回。",
                "close": "确认结束即时抽奖？已完成的抽奖保留结果，已消耗积分不退回。",
                "announce": "确认重新发送一份公告或结果？发送结果不明时，请先核实群内是否已收到。",
                "fulfill": "确认这份奖品已经人工发放？此操作只登记，不会自动发送奖品。"}
            return await self.panel(actor, row["config"]["title"] + "\n" + notes[data["operation"]],
                [button("确认", "lottery_execute", **data), button("返回活动", "lottery_detail", id=row["id"])],
                group_id, message_id, resume={"action": "lottery_detail", "data": {"id": row["id"]}})
        if action == "lottery_field":
            field = data["field"]
            instructions = {
                "title": "请发送新标题，最多 80 个字符。", "description": "请发送活动说明，最多 500 个字符；发送 - 清空。",
                "entry_cost": "请发送每次消耗的积分整数，0 表示免费报名。即时抽奖须大于 0。",
                "min_balance": "请发送参与前所需积分余额，0 表示无门槛。门槛本身不扣积分。",
                "activity": "请发送：统计天数 活跃天数，例如 7 3 表示最近 7 天至少 3 天获得聊天积分。发送 7 0 关闭条件。",
                "schedule": "请发送：手动，或 定时 2026-12-01 20:00（北京时间）。报名抽奖还可发送：满员 50。",
                "daily_limit": "请发送每人每天最多抽奖次数，1 至 100，北京时间零点重置。",
                "min_participants": "请发送最低成团人数。人数不足时取消活动并退款。",
                "prizes": self.prize_instructions(row["config"]["mode"]),
            }
            return await self.prompt(actor, group_id, data, instructions[field])

    @staticmethod
    def prize_instructions(mode):
        if mode == "instant":
            return ("请逐行发送完整奖池，最后一项数字为中奖概率百分比，所有行合计 100。名称中请勿含空格。\n"
                "积分 100 10 20\n奖品 会员月卡 2 10\n未中奖 70\n"
                "上例：10 份 100 积分，概率 20%；2 份月卡，概率 10%；未中奖 70%。奖品领完后对应概率计为未中奖。")
        return "请逐行发送完整奖品列表，名称中请勿含空格，例如：\n积分 100 3\n奖品 会员月卡 2\n表示 3 份 100 积分和 2 份会员月卡；每场最多 20 个中奖名额。"

    @staticmethod
    def parse_prizes(text, mode):
        prizes = []
        instant = mode == "instant"
        try:
            for line in text.splitlines():
                parts = line.split()
                if not parts:
                    continue
                if parts[0] == "未中奖" and instant and len(parts) == 2:
                    prizes.append({"name": "未中奖", "kind": "none", "quantity": 0, "points": 0, "weight": int(parts[1])})
                    continue
                if parts[0] not in ("积分", "奖品") or len(parts) != (4 if instant else 3):
                    raise ValueError()
                points = int(parts[1]) if parts[0] == "积分" else 0
                prizes.append({"name": f"{points} 积分" if points else parts[1], "kind": "points" if parts[0] == "积分" else "manual",
                    "points": points, "quantity": int(parts[2]), "weight": int(parts[3]) if instant else 1})
            if instant and sum(p["weight"] for p in prizes) != 100:
                raise ValueError()
        except (ValueError, IndexError) as exc:
            raise ManagementError(422, "奖品格式有误，请按示例逐行填写；即时抽奖各项概率合计须为 100。") from exc
        return prizes

    async def input(self, actor, group_id, text, data, request_id):
        field = data["field"]
        if field == "new_title":
            config = LotteryConfig(title=text, mode=data["mode"], entry_cost=data["cost"],
                                   prizes=[{"name": "待设置奖品", "kind": "manual"}]).model_dump()
            return await self.prompt(actor, group_id, {"field": "new_prizes", "config": config}, self.prize_instructions(data["mode"]))
        if field == "new_prizes":
            config = {**data["config"], "prizes": self.parse_prizes(text, data["config"]["mode"])}
            row = await self.admin.save(actor, group_id, LotterySave(config=config), request_id)
        else:
            row = await self.admin.get(actor, group_id, data["id"])
            config = dict(row["config"])
            try:
                if field == "prizes":
                    config[field] = self.parse_prizes(text, config["mode"])
                elif field == "activity":
                    config["active_window_days"], config["min_active_days"] = map(int, text.split())
                elif field == "schedule":
                    parts = text.split(maxsplit=1)
                    if parts[0] == "手动" and len(parts) == 1:
                        config.update(draw_mode="manual", ends_at=0)
                    elif parts[0] == "满员" and len(parts) == 2:
                        config.update(draw_mode="participants", participant_target=int(parts[1]), ends_at=0)
                    elif parts[0] == "定时" and len(parts) == 2:
                        stamp = datetime.strptime(parts[1], "%Y-%m-%d %H:%M").replace(tzinfo=BEIJING).timestamp()
                        config.update(draw_mode="time", ends_at=stamp)
                    else:
                        raise ValueError()
                elif field in ("entry_cost", "min_balance", "daily_limit", "min_participants"):
                    config[field] = int(text)
                elif field in ("title", "description"):
                    config[field] = "" if field == "description" and text == "-" else text
                else:
                    raise ValueError()
            except ValueError as exc:
                raise ManagementError(422, "输入格式有误，请按提示填写。") from exc
            row = await self.admin.save(actor, group_id, LotterySave(config=config, version=data["version"]), request_id, row["id"])
        await self.remember(f"bot_prompt:{actor}", None)
        return await self.detail(actor, group_id, row["id"])
