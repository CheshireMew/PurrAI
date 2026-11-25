"""Simplified/traditional equivalence follows saved command data, including custom aliases."""
import hashlib

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from purrai.auto_replies import matching_reply
from purrai.chat_commands import ad_key, canonical, utility_command
from purrai.chat_state import ignored_text
from purrai.command_text import normalized_command
from purrai.group_menu import matched_action
from purrai.group_menu_schemas import MenuConfig
from purrai.models import Action, PointsAccount, PointsEntry
from purrai.points_bot import command
from purrai.points_policy import default_policy
from purrai.points_schemas import PointsSettings
from test_group_menu import button, incoming, operate, save
from test_points_bot import admin_commands, configure, flush, user_message


@pytest.mark.parametrize("value,operation,args", [("簽到", "signin", []), ("簽到加積分", "signin", []),
    ("我的積分", "balance", []), ("積分排行榜", "ranking", []), ("發積分紅包 10 2", "packet", ["10", "2"])])
def test_existing_points_aliases_accept_traditional(value, operation, args):
    assert command(value, default_policy()) == (operation, args)


async def test_traditional_signin_balance_and_ranking_use_real_points_flow(client, env):
    _, db, tg, service = env
    await configure(client, chat_points=0)
    from purrai.points_bot import PointsBot
    bot = PointsBot(db, tg)
    for identifier, phrase in [(960, "簽到"), (960, "簽到"), (961, "積分"), (962, "積分排行榜")]:
        assert await bot.observe(user_message(identifier, phrase))
    await flush(service, db)
    async with db.sessions() as session:
        assert await session.scalar(select(PointsAccount.balance)) == 10
        assert len((await session.scalars(select(PointsEntry))).all()) == 1
        row = await session.scalar(select(Action).where(Action.dedupe_key == "points:-1001:960"))
        assert row.payload["message"]["text"] == "簽到"
    replies = [data.get("text", "") for method, data in tg.calls if method == "send_message"]
    assert any("你当前有 10 积分" in value for value in replies)
    assert any("积分榜" in value for value in replies)


async def test_custom_admin_aliases_and_compact_amount_accept_both_scripts(client, env):
    _, _, tg, _ = env
    aliases = await admin_commands(client, tg, add=["活動獎勵"], subtract=["活动扣分"], lookup=["查詢記錄"])
    policy = PointsSettings.model_validate((await configure(client, commands=aliases))["config"])
    assert command("活动奖励５０", policy) == ("add", ["50"])
    assert command("活動扣分 10", policy) == ("subtract", ["10"])
    assert command("查询记录", policy) == ("lookup", [])


async def test_custom_menu_labels_and_lottery_accept_traditional(client, env):
    _, db, tg, _ = env
    await save(client, [[button("每日签到", "signin"), button("查看积分", "balance"), button("抽奖", "lottery")]])
    await operate(client)
    from conftest import drain
    await drain(env[3])
    async with db.sessions() as session:
        assert await matched_action(session, -1001, "每日簽到") == "signin"
        assert await matched_action(session, -1001, "查看積分") == "balance"
    from purrai.lottery_bot import LotteryBot
    from purrai.telegram import telegram_data
    assert await LotteryBot(db, tg).observe(telegram_data(incoming(963, "抽獎")))


def test_reply_keywords_and_ignored_labels_support_custom_script_equivalence():
    rule = {"keyword": "活動說明", "reply": "原樣顯示", "mode": "exact", "priority": 0, "enabled": True}
    assert matching_reply("活动说明", [rule]) == rule
    assert matching_reply("活動說明", [rule]) == rule
    assert ignored_text("每日領取獎勵", ["每日领取奖励"])
    assert utility_command("積分紅包 10 2")
    assert canonical(" 簽 到 ") == canonical("签到")
    assert ad_key("繁體廣告") == hashlib.sha256("繁體廣告".encode()).hexdigest()


def test_equivalent_alias_collisions_cannot_bind_different_actions():
    config = default_policy().model_dump()
    config["commands"].update(signin=["活動領取"], balance=["活动领取"])
    with pytest.raises(ValidationError):
        PointsSettings.model_validate(config)
    with pytest.raises(ValidationError):
        MenuConfig(rows=[[button("活動領取", "signin"), button("活动领取", "balance")]])
    assert normalized_command("活動領取　ＡＢＣ") == "活动领取 abc"
