import asyncio

import pytest

from aiogram.types import Message
from sqlalchemy import func, select

from conftest import drain, message
from purrai.models import Action, PointsAccount, PointsClaim, PointsEntry, PointsPacket, now
from purrai.points_bot import PointsBot
from purrai.points_rewards import refund_expired
from purrai.runtime import Runtime


async def configure(client, **changes):
    path = "/api/groups/-1001/points/settings"
    doc = (await client.get(path)).json()
    doc["config"].update(enabled=True, **changes)
    response = await client.put(path, json=doc, headers={"Idempotency-Key": f"settings-{doc['version']}"})
    assert response.status_code == 200, response.text
    return response.json()


def user_message(identifier, text, user_id=42, **options):
    return message(identifier, text, user_id=user_id,
        **{"from": {"id": user_id, "is_bot": False, "first_name": f"群友{user_id}"}}, **options)


async def flush(service, db):
    async with db.transaction() as session:
        for action in await session.scalars(select(Action).where(Action.kind == "points_message", Action.state == "pending")):
            action.available_at = 0
    await drain(service)


async def balance(db, user_id=42):
    async with db.sessions() as session:
        return await session.scalar(select(PointsAccount.balance).where(PointsAccount.user_id == user_id))


@pytest.mark.parametrize("advertisement", [True, False])
async def test_chat_reward_waits_for_independent_ad_review(client, env, advertisement):
    from test_advertisement_review import reviewer
    _, db, tg, service = env
    await configure(client)
    worker, _ = await reviewer(env, lambda _: advertisement)
    runtime = Runtime(db, tg, service)
    await runtime.events.message(Message.model_validate(user_message(81, "一起看海边日落")))
    await flush(service, db)
    assert not await balance(db)
    async with db.transaction() as session:
        reward = await session.scalar(select(Action).where(Action.kind == "points_message"))
        reward.payload = {**reward.payload, "expires_at": now() - 1}
    assert await worker.run_one()
    await drain(service)
    assert (await balance(db) or 0) == (0 if advertisement else 1)
    assert any(method == "delete_message" for method, _ in tg.calls) == advertisement
    await runtime.stop()


@pytest.mark.parametrize("learned_late", [False, True])
async def test_local_only_review_settles_reward_once_window_closes(client, env, monkeypatch, learned_late):
    from purrai import advertisement_decisions, advertisement_intake, advertisement_review
    from test_advertisement_review import add_sample
    _, db, tg, service = env
    await configure(client)
    runtime = Runtime(db, tg, service)
    msg = user_message(82, "这是一条可独立核对来源的测试发言内容")
    await runtime.events.message(Message.model_validate(msg))
    await flush(service, db)
    assert not await balance(db)
    if learned_late:
        await add_sample(env, msg["text"])
    for module in (advertisement_review, advertisement_intake, advertisement_decisions):
        monkeypatch.setattr(module, "now", lambda: msg["date"] + 47 * 3600 + 1)
    assert await service.advertisement_review.run_one()
    await drain(service)
    assert (await balance(db) or 0) == (0 if learned_late else 1)
    assert not any(method == "delete_message" for method, _ in tg.calls)
    await runtime.stop()


async def test_supergroup_reused_message_number_earns_separately(client, env):
    from purrai.group_migration import receive_migration
    _, db, tg, service = env
    await configure(client)
    bot = PointsBot(db, tg)
    await bot.observe(user_message(15, "第一次合格发言"))
    await flush(service, db)
    async with db.transaction() as session:
        assert await receive_migration(session, {"message": {"chat": {"id": -1001}, "migrate_to_chat_id": -2002}})
    new = user_message(15, "新群合格发言", user_id=43, chat={"id": -2002, "type": "supergroup"})
    for _ in range(2):
        await bot.observe(new)
        await flush(service, db)
    assert await balance(db, 42) == await balance(db, 43) == 1
    async with db.sessions() as session:
        keys = set(await session.scalars(select(PointsEntry.request_id).where(PointsEntry.group_id == -2002)))
        assert keys == {"chat:-1001:15", "chat:-2002:15"}


async def test_real_events_signin_alias_query_rank_and_replay(client, env):
    _, db, tg, service = env
    await configure(client)
    runtime = Runtime(db, tg, service)
    for identifier, text in [(1, "签到"), (1, "签到"), (2, "签到加积分"), (3, "积分"), (4, "积分排行")]:
        await runtime.events.message(Message.model_validate(user_message(identifier, text)))
        await drain(service)
    assert await balance(db) == 10
    replies = [args["text"] for method, args in tg.calls if method == "send_message"]
    assert sum("签到成功" in text for text in replies) == 1
    assert any("已经签到" in text for text in replies)
    assert any("当前有 10" in text for text in replies)
    assert any("积分榜" in text and "群友42" in text for text in replies)
    await runtime.stop()


async def test_configurable_commands_replace_disable_without_reseed(client, env):
    _, db, tg, service = env
    doc = await configure(client, chat_points=0)
    bot = PointsBot(db, tg)
    original = doc["config"]["commands"]["signin"][0]
    doc["config"]["commands"]["signin"] = ["任意测试口令甲"]
    await configure(client, commands=doc["config"]["commands"], chat_points=0)
    assert not await bot.observe(user_message(10, original))
    assert await bot.observe(user_message(11, "任意测试口令甲"))
    await drain(service)
    assert await balance(db) == 10
    doc["config"]["commands"]["signin"] = ["任意测试口令乙"]
    await configure(client, commands=doc["config"]["commands"], chat_points=0)
    assert not await bot.observe(user_message(12, "任意测试口令甲"))
    assert await bot.observe(user_message(13, "任意测试口令乙", user_id=43))
    await drain(service)
    assert await balance(db, 43) == 10
    doc["config"]["commands"]["signin"] = []
    await configure(client, commands=doc["config"]["commands"], chat_points=0)
    assert not await bot.observe(user_message(14, "任意测试口令乙", user_id=44))


async def test_chat_awards_limit_duplicates_forward_and_moderation(client, env):
    _, db, tg, service = env
    await configure(client, chat_daily_limit=2, chat_interval_seconds=10)
    bot = PointsBot(db, tg)
    timestamp = int(now()) - 70
    for index, text in enumerate(["今天天气真好", "今天天气真好", "我们一起出去玩", "欢迎新朋友"]):
        await bot.observe(user_message(20 + index, text, date=timestamp + index * 20))
        await flush(service, db)
    assert await balance(db) == 2
    forwarded = user_message(30, "这是一条转发消息", user_id=43,
        forward_origin={"type": "user", "date": timestamp, "sender_user": {"id": 44, "is_bot": False, "first_name": "x"}})
    assert not await bot.observe(forwarded)
    assert not await bot.observe(user_message(31, "编辑内容不能赚分", user_id=43), edited=True)
    from purrai.models import Violation
    async with db.transaction() as session:
        session.add(Violation(group_id=-1001, message_id=32, user_id=43, reasons=["已被群规则拦截"]))
    await bot.observe(user_message(32, "签到", user_id=43))
    await flush(service, db)
    assert await balance(db, 43) is None


async def test_signin_day_is_shanghai_and_zero_reward_stays_zero(client, env):
    from datetime import datetime, timezone
    from purrai.points_rewards import earn, user_account
    from purrai.points_policy import get_policy
    _, db, _, _ = env
    await configure(client)
    before = datetime(2026, 11, 16, 15, 59, tzinfo=timezone.utc).timestamp()
    after = before + 120
    async with db.transaction() as session:
        account = await user_account(session, -1001, {"id": 42, "first_name": "测试"})
        policy = await get_policy(session, -1001)
        for stamp in [before, before, after]:
            await earn(session, account, policy, user_message(40, "签到", date=stamp), "signin")
    assert await balance(db) == 20
    await configure(client, signin_points=0)
    async with db.transaction() as session:
        account = await user_account(session, -1001, {"id": 43, "first_name": "测试"})
        await earn(session, account, await get_policy(session, -1001), user_message(41, "签到", date=after), "signin")
    assert await balance(db, 43) == 0


def callback(packet, user_id):
    return {"id": f"claim-{packet}-{user_id}", "data": f"pts:claim:{packet}",
        "from": {"id": user_id, "first_name": f"群友{user_id}", "is_bot": False},
        "message": {"message_id": 99, "chat": {"id": -1001, "type": "supergroup"}}}


async def test_packet_debit_concurrent_claims_duplicate_and_conservation(client, env):
    _, db, tg, service = env
    await configure(client)
    bot = PointsBot(db, tg)
    await bot.observe(user_message(50, "签到"))
    await drain(service)
    await bot.observe(user_message(51, "积分红包 7 3"))
    await drain(service)
    await bot.observe(user_message(51, "积分红包 7 3"))
    await drain(service)
    assert await balance(db) == 3
    async with db.sessions() as session:
        packet_id = await session.scalar(select(PointsPacket.id))
    await asyncio.gather(*(bot.callback(callback(packet_id, i)) for i in (43, 44, 45, 46)))
    await bot.callback(callback(packet_id, 43))
    async with db.sessions() as session:
        assert await session.scalar(select(func.sum(PointsAccount.balance))) == 10
        assert await session.scalar(select(func.count()).select_from(PointsClaim)) == 3
        assert await session.scalar(select(func.sum(PointsClaim.points))) == 7
        assert (await session.get(PointsPacket, packet_id)).remaining == 0
        for account in await session.scalars(select(PointsAccount)):
            assert await session.scalar(select(func.sum(PointsEntry.delta)).where(PointsEntry.account_id == account.id)) == account.balance


async def test_target_membership_insufficient_balance_and_expiry_refund(client, env):
    _, db, tg, service = env
    await configure(client)
    bot = PointsBot(db, tg)
    await bot.observe(user_message(60, "签到"))
    await drain(service)
    await bot.observe(user_message(61, "积分红包 11 1"))
    await drain(service)
    async with db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(PointsPacket)) == 0
    target = {"message_id": 9, "from": {"id": 43, "is_bot": False, "first_name": "收件人"}}
    await bot.observe(user_message(62, "积分红包 8", reply_to_message=target))
    await drain(service)
    async with db.sessions() as session:
        packet_id = await session.scalar(select(PointsPacket.id))
    await bot.callback(callback(packet_id, 44))
    tg.members[(-1001, 43)] = {"status": "left"}
    await bot.callback(callback(packet_id, 43))
    assert await balance(db) == 2
    async with db.transaction() as session:
        packet = await session.get(PointsPacket, packet_id)
        packet.expires_at = now() - 1
        await refund_expired(session)
        await refund_expired(session)
    assert await balance(db) == 10
    tg.members[(-1001, 43)] = {"status": "member"}
    await bot.callback(callback(packet_id, 43))
    assert await balance(db, 43) is None


async def admin_commands(client, tg, **changes):
    doc = await configure(client, chat_points=0)
    commands = {**doc["config"]["commands"], "add": ["添加积分"], "subtract": ["减少积分"], "lookup": ["查积分"], **changes}
    await configure(client, commands=commands, chat_points=0)
    tg.members[(-1001, 1)] = {"status": "administrator"}
    return commands


def target_reply(user_id=43, **options):
    return {"message_id": 9, "date": int(now()) - 10, "chat": {"id": -1001, "type": "supergroup"},
        "from": {"id": user_id, "is_bot": False, "first_name": f"目标{user_id}"}, **options}


@pytest.mark.parametrize("separator", [" ", ""])
async def test_admin_reply_adjust_lookup_replay_and_ledger(client, env, separator):
    _, db, tg, service = env
    await admin_commands(client, tg)
    runtime = Runtime(db, tg, service)
    for identifier, text in [(70, f"添加积分{separator}100"), (70, f"添加积分{separator}100"),
                             (71, f"减少积分{separator}30"), (72, "查积分")]:
        await runtime.events.message(Message.model_validate(user_message(identifier, text,
            user_id=42 if text == "查积分" else 1, reply_to_message=target_reply())))
        await drain(service)
    assert await balance(db, 43) == 70
    assert await balance(db, 1) is None
    async with db.sessions() as session:
        rows = list(await session.scalars(select(PointsEntry).order_by(PointsEntry.created_at)))
        assert [r.delta for r in rows] == [100, -30]
        assert all(r.actor_id == 1 and r.kind == "adjustment" and "目标消息 9" in r.reason for r in rows)
    replies = [args["text"] for method, args in tg.calls if method == "send_message"]
    assert any("目标43 当前有 70" in text for text in replies)
    await runtime.events.message(Message.model_validate(user_message(73, "查积分", user_id=43)))
    await drain(service)
    assert any("群友43 当前有 70" in args.get("text", "") for method,args in tg.calls if method == "send_message")
    await runtime.stop()


@pytest.mark.parametrize("separator", [" ", ""])
async def test_admin_permission_is_current_and_lookup_cannot_credit(client, env, separator):
    _, db, tg, service = env
    await admin_commands(client, tg)
    bot = PointsBot(db, tg)
    for i, text in enumerate([f"添加积分{separator}100", f"减少积分{separator}1", "查积分"]):
        await bot.observe(user_message(80+i, text, reply_to_message=target_reply()))
    await drain(service)
    assert await balance(db, 43) is None and await balance(db, 42) is None
    assert sum("只有本群管理员" in args.get("text", "") for method,args in tg.calls if method == "send_message") == 2
    assert any("目标43 当前有 0" in args.get("text", "") for method,args in tg.calls if method == "send_message")
    await bot.observe(user_message(84, "添加积分 100", user_id=1, reply_to_message=target_reply()))
    tg.members[(-1001, 1)] = {"status": "member"}
    await drain(service)
    assert await balance(db, 43) is None
    await bot.observe(user_message(85, "查积分"))
    await drain(service)
    assert await balance(db, 42) is None
    assert "群友42 当前有 0" in [args["text"] for method,args in tg.calls if method == "send_message"][-1]


async def test_admin_bad_amounts_reply_targets_and_insufficient_points(client, env):
    _, db, tg, service = env
    await admin_commands(client, tg)
    bot = PointsBot(db, tg)
    inputs = [(f"添加积分{suffix}", target_reply()) for suffix in ["", " 0", " -10", " +10", " 1.5", " 9007199254740992", " 1 2",
        "0", "-10", "+10", "1.5", "9007199254740992", "1 2", "40元", "40还是50"]]
    inputs += [("添加积分 10", reply) for reply in [None, target_reply(**{"from": {"id": 999,"is_bot": True}}),
        target_reply(sender_chat={"id": -1001}), target_reply(chat={"id": -1002})]]
    inputs += [("查积分 10", target_reply())]
    for i,(text, reply) in enumerate(inputs):
        await bot.observe(user_message(90+i, text, user_id=1, **({"reply_to_message":reply} if reply else {})))
        await drain(service)
    async with db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(PointsAccount)) == 0
        assert await session.scalar(select(func.count()).select_from(PointsEntry)) == 0
    for i,text in enumerate(["添加积分 10", "减少积分 11"]):
        await bot.observe(user_message(110+i,text,user_id=1,reply_to_message=target_reply()))
        await drain(service)
    assert await balance(db,43)==10
    assert any("积分不足" in args.get("text", "") for method,args in tg.calls if method == "send_message")


@pytest.mark.parametrize(("operation", "separator"), [("add", " "), ("subtract", " "), ("lookup", ""),
                                                     ("add", ""), ("subtract", "")])
async def test_reply_commands_configurable_replace_disable_and_pending(client, env, operation, separator):
    from purrai.points_bot import command
    from purrai.points_schemas import PointsSettings
    _,db,tg,service=env
    commands=await admin_commands(client,tg)
    bot=PointsBot(db,tg)
    old=commands[operation][0]
    suffix="" if operation=="lookup" else separator+"1"
    commands[operation]=["任意口令甲"]
    await configure(client,commands=commands,chat_points=0)
    assert not await bot.observe(user_message(120,old+suffix,user_id=1,reply_to_message=target_reply()))
    assert await bot.observe(user_message(121,"任意口令甲"+suffix,user_id=1,reply_to_message=target_reply()))
    await drain(service)
    replies=[args["text"] for method,args in tg.calls if method=="send_message"]
    assert replies and ("+1" in replies[-1] if operation=="add" else "积分不足" in replies[-1] if operation=="subtract" else "当前有 0" in replies[-1])
    commands[operation]=["任意口令乙"]
    doc=await configure(client,commands=commands,chat_points=0)
    assert command(old+suffix,PointsSettings.model_validate(doc['config']))[0] is None
    assert not await bot.observe(user_message(122,"任意口令甲"+suffix,user_id=1,reply_to_message=target_reply()))
    assert await bot.observe(user_message(123,"任意口令乙"+suffix,user_id=1,reply_to_message=target_reply()))
    commands[operation]=[]
    await configure(client,commands=commands,chat_points=0)
    await drain(service)
    assert not await bot.observe(user_message(124,"任意口令乙"+suffix,user_id=1,reply_to_message=target_reply()))
    async with db.sessions() as session:
        action=await session.scalar(select(Action).where(Action.dedupe_key=="points:-1001:123"))
        assert action.state=="cancelled"


async def test_compact_command_alias_precedence_and_pending_amount_change(client, env):
    from purrai.points_bot import command
    from purrai.points_schemas import PointsSettings
    _, db, tg, service = env
    commands = await admin_commands(client, tg, add=["任意校对", "任意校对1"], lookup=["任意校对140"])
    doc = await configure(client, commands=commands, chat_points=0)
    policy = PointsSettings.model_validate(doc["config"])
    assert command("任意校对140", policy) == ("lookup", [])
    assert command("任意校对150", policy) == ("add", ["50"])
    assert command("任意校对1 50", policy) == ("add", ["50"])
    assert command("任意校对１　５０", policy) == ("add", ["50"])
    assert command("任意校对１５０", policy) == ("add", ["50"])
    bot = PointsBot(db, tg)
    assert await bot.observe(user_message(150, "任意校对150", user_id=1, reply_to_message=target_reply()))
    commands["add"] = ["任意校对"]
    await configure(client, commands=commands, chat_points=0)
    await drain(service)
    assert await balance(db, 43) is None
    async with db.sessions() as session:
        action = await session.scalar(select(Action).where(Action.dedupe_key == "points:-1001:150"))
        assert action.state == "cancelled"


async def test_admin_permission_network_failure_and_send_failure_no_duplicate(client, env):
    _,db,tg,service=env
    await admin_commands(client,tg)
    bot=PointsBot(db,tg)
    tg.fail['get_chat_member']=1
    await bot.observe(user_message(130,'添加积分 20',user_id=1,reply_to_message=target_reply()))
    await drain(service)
    assert await balance(db,43) is None
    tg.fail['send_message']=1
    await flush(service,db)
    assert await balance(db,43)==20
    await bot.observe(user_message(130,'添加积分 20',user_id=1,reply_to_message=target_reply()))
    await flush(service,db)
    assert await balance(db,43)==20
    async with db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(PointsEntry))==1


async def test_admin_config_disabled_during_permission_check(client, env):
    from purrai.models import PointsPolicy
    _,db,tg,service=env
    await admin_commands(client,tg)
    original=tg.call
    async def disable(method, **kwargs):
        if method=="get_chat_member":
            async with db.transaction() as session:
                policy=await session.get(PointsPolicy,-1001)
                policy.config={**policy.config,"enabled":False}
        return await original(method,**kwargs)
    tg.call=disable
    await PointsBot(db,tg).observe(user_message(140,"添加积分 20",user_id=1,reply_to_message=target_reply()))
    await drain(service)
    assert await balance(db,43) is None


@pytest.mark.parametrize("unchanged", [True, False])
async def test_ranking_callback_completes_unchanged_page_but_retries_real_edit_errors(client, env, monkeypatch, unchanged):
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import EditMessageText
    from purrai.models import Inbox

    _, db, tg, service = env
    await configure(client)
    runtime = Runtime(db, tg, service)
    original = tg.call
    error = "message is not modified" if unchanged else "message to edit not found"

    async def edit_error(method, **kwargs):
        if method == "edit_message_text":
            raise TelegramBadRequest(method=EditMessageText(**kwargs), message=error)
        return await original(method, **kwargs)

    monkeypatch.setattr(tg, "call", edit_error)
    query = {"id": "ranking-repeat", "chat_instance": "test", "data": "pts:rank:1",
             "from": {"id": 42, "is_bot": False, "first_name": "Test"},
             "message": {"message_id": 90, "date": int(now()),
                         "chat": {"id": -1001, "type": "supergroup"}}}
    try:
        await runtime.persist([{"update_id": 9001, "callback_query": query}])
        assert await runtime.process_one("messages")
        async with db.sessions() as session:
            event = await session.get(Inbox, 9001)
            assert event.state == ("done" if unchanged else "pending")
            assert not event.error if unchanged else error in event.error
            assert await session.scalar(select(func.count()).select_from(PointsEntry)) == 0
        assert any(method == "answer_callback_query" for method, _ in tg.calls) == unchanged
    finally:
        await runtime.stop()
