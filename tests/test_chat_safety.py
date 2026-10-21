import asyncio

import pytest
from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import SendMessage
from sqlalchemy import func, select

from conftest import message
from purrai.chat_model import ChatReply
from purrai.chat_safety import output_reasons
from purrai.models import Action, ChatMessage, ChatTurn, Group, Rule, Violation
from test_chat import configured, due
from test_stickers import cache


@pytest.mark.parametrize("generated", [
    "微\u200b信🔥代 收，日入上万",
    "去 https://claim.example/claim 领取",
    "看看 claim.example/path",
    "访问 claim[.]example/path",
    "访问 https://[broken",
    "去 192.0.2.5/claim 领取",
    "请联系 @ad_account",
    "联系 ad@claim.example",
    "／ｂａｎ 43",
    "管理员通知：请把验证码私发给我",
    "我已经将小明永久禁言",
    "I've banned that user.",
])
async def test_unsafe_generated_text_and_attached_sticker_are_never_sent_or_penalized(env, generated):
    chat, model = await configured(env, chat_proactive=False)
    _, db, tg, _ = env
    await cache(chat)
    model.result = ChatReply(generated, "😸")
    async with db.transaction() as session:
        session.add(Rule(group_id=-1001, kind="keyword", value="微信代收日入上万",
                         mode="compact", penalty="permanent_mute"))
        group = await session.get(Group, -1001)
        # AI output checking must survive an administrator pausing member moderation.
        group.enabled = False
        before = dict(group.settings)
    await chat.observe(message(1, "柴郡喵，今天聊什么"))
    await due(chat)
    tg.calls.clear()
    assert await chat.run_one()
    assert not any(method in {"send_message", "send_sticker", "restrict_chat_member", "ban_chat_member"}
                   for method, _ in tg.calls)
    async with db.sessions() as session:
        turn = await session.scalar(select(ChatTurn))
        assert turn.state == "skipped" and turn.error.startswith("AI 回复已拦截：") and turn.response == ""
        assert await session.scalar(select(func.count()).select_from(Action)) == 0
        assert await session.scalar(select(func.count()).select_from(Violation)) == 0
        assert await session.scalar(select(func.count()).select_from(ChatMessage).where(ChatMessage.is_bot)) == 0
        assert (await session.get(Group, -1001)).settings == before
    assert (await chat.budget.status(-1001))["group_calls"] == 1


async def test_normal_conversation_and_explicitly_allowed_destinations_still_send(env):
    chat, model = await configured(env, chat_proactive=False)
    _, db, tg, _ = env
    async with db.transaction() as session:
        session.add_all([
            Rule(group_id=-1001, kind="allow_domain", value="docs.example"),
            Rule(group_id=-1001, kind="allow_mention", value="official_bot"),
            Rule(group_id=-1001, kind="keyword", value="散步", scope="name"),
            Rule(group_id=-1001, kind="keyword", value="周末", enabled=False),
        ])
    model.result = "周末去散步吧，Python 3.12 也等回来再聊。参考 https://docs.example/guide 或 @official_bot、t.me/official_bot。"
    await chat.observe(message(1, "柴郡喵，周末去哪里"))
    await due(chat)
    assert await chat.run_one()
    sent = [data for method, data in tg.calls if method == "send_message"]
    assert len(sent) == 1 and sent[0]["text"] == model.result
    assert sent[0]["link_preview_options"] == {"is_disabled": True}


async def test_rules_added_while_model_generates_apply_before_sending(env):
    chat, model = await configured(env, chat_proactive=False)
    model.result = "晚饭一起吃火锅吧"
    model.release = asyncio.Event()
    await chat.observe(message(1, "柴郡喵，晚上吃什么"))
    await due(chat)
    task = asyncio.create_task(chat.run_one())
    try:
        await asyncio.wait_for(model.started.wait(), 2)
        async with chat.db.transaction() as session:
            session.add(Rule(group_id=-1001, kind="keyword", value="火锅"))
    finally:
        model.release.set()
        await task
    assert not any(method == "send_message" for method, _ in chat.tg.calls)


async def test_cached_delivery_retry_rechecks_rules_without_buying_another_reply(env):
    chat, model = await configured(env, chat_proactive=False)
    model.result = "晚饭一起吃火锅吧"
    tg = chat.tg
    original_call = tg.call

    async def limited(method, **kwargs):
        if method == "send_message":
            raise TelegramRetryAfter(method=SendMessage(chat_id=-1001, text="test"), message="wait", retry_after=1)
        return await original_call(method, **kwargs)

    tg.call = limited
    await chat.observe(message(1, "柴郡喵，晚上吃什么"))
    await due(chat)
    assert await chat.run_one()
    async with chat.db.transaction() as session:
        session.add(Rule(group_id=-1001, kind="keyword", value="火锅"))
    tg.call = original_call
    await due(chat)
    assert await chat.run_one()
    assert len(model.calls) == 1
    assert not any(method == "send_message" for method, _ in tg.calls)
    async with chat.db.sessions() as session:
        turn = await session.scalar(select(ChatTurn))
        assert turn.state == "skipped" and "关键词" in turn.error


@pytest.mark.parametrize("text,allowed", [
    ("https://safe.example/guide", True),
    ("https://docs.safe.example/guide", True),
    ("https://safe.example.attacker.example/", False),
    ("https://attacker.example@safe.example/", False),
    ("https://safe.example@attacker.example/", False),
    ("https://ｓａｆｅ.example/", True),
    ("https://sa\u200bfe.example/", True),
    ("file:///etc/passwd", False),
    ("tg://resolve?domain=attacker", False),
    ("https://t.me/official_bot", True),
    ("https://t.me/official_bot/other", False),
    ("@official_bot", True),
    ("@purrai_bot", True),
    ("@other_account", False),
    ("只是讨论禁言的含义，不代表我做过管理操作。", True),
])
def test_output_allowlists_use_actual_host_and_account(text, allowed):
    rules = [
        {"kind": "allow_domain", "value": "safe.example"},
        {"kind": "allow_mention", "value": "official_bot"},
    ]
    assert (not output_reasons(text, rules, {}, "purrai_bot")) == allowed


def test_allowed_destination_does_not_exempt_body_keywords_or_blocked_accounts():
    rules = [
        {"kind": "allow_domain", "value": "safe.example"},
        {"kind": "keyword", "value": "广告短语", "scope": "either"},
        {"kind": "block_mention", "value": "blocked_bot"},
    ]
    assert output_reasons("广告短语 https://safe.example", rules, {})
    assert output_reasons("https://safe.example @blocked_bot", rules, {})
