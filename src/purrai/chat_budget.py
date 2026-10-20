"""Atomic daily reservations; uncertain calls keep their reserved allowance."""

import json
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from purrai.models import Meta, now

KEY = "private:chat-budget"


class BudgetExceeded(Exception):
    pass


@dataclass(frozen=True)
class Reservation:
    day: str
    group: str
    user: str
    tokens: int


class ChatBudget:
    def __init__(self, db, settings):
        self.db, self.settings = db, settings

    @staticmethod
    def day():
        return datetime.fromtimestamp(now(), ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")

    @classmethod
    async def load(cls, session):
        row = await session.get(Meta, KEY)
        data = json.loads(row.value) if row else {}
        if data.get("day") != cls.day():
            data = {"day": cls.day(), "global": {}, "groups": {}, "users": {}}
        return row, data

    @staticmethod
    def save(session, row, data):
        value = json.dumps(data, separators=(",", ":"))
        if row:
            row.value = value
        else:
            session.add(Meta(key=KEY, value=value))

    async def reserve(self, group_id, user_id, system, payload, turn_id=None):
        # UTF-8 bytes + framing + the server-enforced output bound deliberately
        # over-reserve common BPE inputs. This is a local budget, not a currency bill.
        tokens = len(system.encode("utf-8")) + len(payload.encode("utf-8")) + 512 + self.settings.chat_max_tokens
        group_key, user_key = str(group_id), str(user_id)
        async with self.db.transaction() as session:
            from purrai.models import Group

            group = await session.get(Group, group_id)
            if not group or not group.approved or not group.settings.get("chat_enabled"):
                raise BudgetExceeded("此群尚未获准使用聊天，或聊天已关闭")
            if turn_id is not None:
                from purrai.models import ChatTurn

                turn = await session.get(ChatTurn, turn_id)
                reset = await session.get(Meta, f"chat_reset:{group_id}")
                if not turn or turn.state != "generating" or reset and float(reset.value) > turn.created_at:
                    raise BudgetExceeded("聊天任务已取消，不再调用模型")
            row, data = await self.load(session)
            counters = [data["global"], data["groups"].setdefault(group_key, {}),
                        data["users"].setdefault(user_key, {})]
            cfg = self.settings
            bounds = [(cfg.chat_daily_calls, cfg.chat_daily_tokens, 0, "全部群"),
                      (cfg.chat_group_daily_calls, cfg.chat_group_daily_tokens, cfg.chat_group_interval, "本群"),
                      (cfg.chat_user_daily_calls, cfg.chat_user_daily_tokens, cfg.chat_user_interval, "这位成员")]
            for counter, (calls, limit, interval, label) in zip(counters, bounds):
                if counter.get("calls", 0) >= calls or counter.get("tokens", 0) + tokens > limit:
                    raise BudgetExceeded(f"{label}今日 AI 聊天额度已用完；明日恢复，验证和防广告不受影响")
                if now() - counter.get("last_attempt", 0) < interval:
                    raise BudgetExceeded(f"{label}调用太频繁，请稍后再聊；验证和防广告不受影响")
            for counter in counters:
                counter.update(calls=counter.get("calls", 0) + 1,
                               tokens=counter.get("tokens", 0) + tokens, last_attempt=now())
            self.save(session, row, data)
            return Reservation(data["day"], group_key, user_key, tokens)

    async def settle(self, reservation, actual_tokens):
        if type(actual_tokens) is not int or actual_tokens < 0:
            return  # Missing/uncertain usage stays reserved, including failures and crashes.
        async with self.db.transaction() as session:
            row, data = await self.load(session)
            if not row or data["day"] != reservation.day:
                return
            for counter in (data["global"], data["groups"][reservation.group], data["users"][reservation.user]):
                counter["tokens"] = max(0, counter["tokens"] + actual_tokens - reservation.tokens)
            self.save(session, row, data)

    async def status(self, group_id):
        async with self.db.sessions() as session:
            _, data = await self.load(session)
        cfg = self.settings
        return {"day": data["day"], "group_calls": data["groups"].get(str(group_id), {}).get("calls", 0),
                "group_tokens": data["groups"].get(str(group_id), {}).get("tokens", 0),
                "group_call_limit": cfg.chat_group_daily_calls, "group_token_limit": cfg.chat_group_daily_tokens,
                "global_call_limit": cfg.chat_daily_calls, "global_token_limit": cfg.chat_daily_tokens,
                "global_calls": data["global"].get("calls", 0), "global_tokens": data["global"].get("tokens", 0),
                "user_call_limit": cfg.chat_user_daily_calls, "user_token_limit": cfg.chat_user_daily_tokens}
