"""Advertising is filtered before requesting a model and before delivery."""

import asyncio
import json

import httpx
import pytest
from sqlalchemy import func, select

from conftest import message
from purrai.chat_model import ChatModel, ChatModelError, ChatReply
from purrai.config import Settings
from purrai.models import Action, Rule, Violation
from test_chat import age_reply, configured


@pytest.mark.parametrize("text", [
    "来个懂跑分，有灰经验优先，水房助理无压",
    "卧槽 我花钱进的策略频道现在直接进了???",
    "@purrai_bot 来个懂跑分，有灰经验优先",
    "柴郡喵，来个懂跑分，有灰经验优先",
    "急招跑 分，日结无押",
    "跑\u200b分招聘，日结无押",
    "带单返佣，看我简介私聊",
    "有搬砖套利项目，联系我了解",
    "零风险稳赚，立即加群注册",
])
async def test_solicitations_never_request_a_model_or_penalize_a_member(env, text):
    chat, model = await configured(env)
    await chat.observe(message(1, text))
    assert not await chat.run_one() and not model.calls
    assert not any(method in ("send_message", "send_sticker") for method, _ in chat.tg.calls)
    async with chat.db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Violation)) == 0
        assert await session.scalar(select(func.count()).select_from(Action)) == 0


@pytest.mark.parametrize("text", [
    "柴郡喵，跑分是骗局吗？",
    "@purrai_bot 跑分招聘是不是骗局？",
    "柴郡喵，如何识别私聊返佣广告？",
    "柴郡喵，积分怎么计算？",
    "柴郡喵，花钱进的频道现在免费了，你怎么看？",
    "柴郡喵，我们招聘前端工程师，怎么写岗位说明？",
])
async def test_real_questions_about_scams_or_normal_recruitment_remain_answerable(env, text):
    chat, model = await configured(env)
    await chat.observe(message(1, text))
    assert await chat.run_one() and len(model.calls) == 1


async def test_configured_rules_apply_to_chat_even_with_member_protection_disabled(env):
    chat, model = await configured(env)
    async with chat.db.transaction() as session:
        session.add(Rule(group_id=-1001, kind="keyword", value="特定招揽业务", mode="compact", scope="message"))
    await chat.observe(message(1, "柴郡喵，特定 招揽业务", user_id=1))
    assert not await chat.run_one() and not model.calls


async def test_hidden_blocked_link_is_checked_before_a_chat_request(env):
    chat, model = await configured(env)
    async with chat.db.transaction() as session:
        session.add(Rule(group_id=-1001, kind="block_domain", value="advert.example"))
    await chat.observe(message(1, "柴郡喵，看这里", entities=[
        {"type": "text_link", "offset": 5, "length": 3, "url": "https://advert.example/invite"}]))
    assert not await chat.run_one() and not model.calls


async def test_split_solicitation_cannot_leave_the_first_part_as_a_reply_target(env):
    chat, model = await configured(env)
    await chat.observe(message(1, "@purrai_bot 来个懂"))
    await chat.observe(message(2, "跑分，有灰经验优先"))
    await chat.run_one()
    assert not model.calls


async def test_new_input_rule_cancels_an_answer_already_being_generated(env):
    chat, model = await configured(env)
    await chat.observe(message(1, "柴郡喵，隐藏招揽内容"))
    model.release = asyncio.Event()
    running = asyncio.create_task(chat.run_one())
    try:
        await asyncio.wait_for(model.started.wait(), 3)
        async with chat.db.transaction() as session:
            session.add(Rule(group_id=-1001, kind="keyword", value="隐藏招揽内容", scope="message"))
    finally:
        model.release.set()
        await running
    assert not any(method in ("send_message", "send_sticker") for method, _ in chat.tg.calls)


async def test_model_advertisement_flag_overrides_reply_and_sticker(env):
    chat, model = await configured(env)
    model.result = ChatReply("不该发送的点评", "😸", should_reply=True, is_advertisement=True)
    await chat.observe(message(1, "柴郡喵，程序还没认出的新型招揽"))
    assert await chat.run_one()
    assert len(model.calls) == 1
    assert not any(method in ("send_message", "send_sticker") for method, _ in chat.tg.calls)


async def test_legitimate_scam_advice_remains_in_the_followup_context(env):
    chat, model = await configured(env)
    model.result = "私聊推返佣的先警惕，别给陌生人垫款。"
    await chat.observe(message(1, "柴郡喵，怎么识别返佣骗局？"))
    await chat.run_one()
    await age_reply(chat)
    await chat.observe(message(2, "然后怎么确认对方的身份？"))
    assert await chat.run_one()
    assert any(entry.get("bot") and "私聊推返佣" in entry["text"] for entry in model.calls[-1][1]["messages"])


@pytest.mark.parametrize("flag", [True, False, None, "false"])
async def test_http_model_requires_an_explicit_advertising_boolean(flag):
    body = {"should_reply": True, "reply": "这句话不能盖过广告判断", "sticker": "😸"}
    if flag is not None:
        body["is_advertisement"] = flag
    model = ChatModel(Settings(chat_api_base="https://model.test/v1", chat_api_key="test", chat_model="test"),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps(body)}}]})))
    try:
        if type(flag) is not bool:
            with pytest.raises(ChatModelError):
                await model.reply("系统", "上下文")
        else:
            result = await model.reply("系统", "上下文")
            assert result.is_advertisement is flag and result.should_reply is not flag
            if flag:
                assert result.text is None and result.sticker is None
    finally:
        await model.close()
