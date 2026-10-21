import asyncio
import json

import httpx
import pytest
from sqlalchemy import func, select, update

from conftest import join, message
from purrai.chat import GroupChat, addressed
from purrai.chat_model import ChatModel, ChatModelError, ChatReply
from purrai.config import Settings
from purrai.models import ArchivedMessage, ChatMessage, ChatTurn, Group, Meta, Rule, default_settings, now
from purrai.runtime import Runtime
from test_bot_admin import click, deliver, markup, open_group, private


class FakeModel:
    def __init__(self, result="这话我同意，周末就该出去走走。"):
        self.result, self.calls, self.payloads = result, [], []
        self.started = asyncio.Event()
        self.release = None

    async def reply(self, system, context):
        self.payloads.append(context)
        self.calls.append((system, json.loads(context)))
        self.started.set()
        if self.release:
            await self.release.wait()
        if isinstance(self.result, Exception):
            raise self.result
        return self.result if isinstance(self.result, ChatReply) else ChatReply(self.result)

    async def close(self):
        pass


async def configured(env, **options):
    settings, db, tg, service = env
    settings.chat_api_base, settings.chat_api_key, settings.chat_model = "https://model.test/v1", "secret", "chat-model"
    tg.username = "purrai_bot"
    async with db.transaction() as session:
        group = await session.get(Group, -1001)
        group.settings = {**group.settings, "chat_enabled": True, **options}
    model = FakeModel()
    chat = GroupChat(db, tg, settings, model=model)
    return chat, model


async def due(chat):
    async with chat.db.transaction() as session:
        await session.execute(update(ChatTurn).where(ChatTurn.state == "pending").values(available_at=0))


async def age_reply(chat, seconds=10):
    async with chat.db.transaction() as session:
        row = await session.get(Meta, "chat_session:-1001:0")
        state = json.loads(row.value)
        state["last_reply"] = now() - seconds
        state["last_attempt"] = now() - seconds
        row.value = json.dumps(state)


async def test_batch_keeps_speakers_and_reply_relationship_and_sends_once(env):
    chat, model = await configured(env)
    _, db, tg, _ = env
    await chat.observe(message(1, "周末去山里走走？", **{"from": {"id": 42, "first_name": "小明"}}))
    request = message(2, "@purrai_bot 你觉得呢", 43, reply_to_message={"message_id": 1, "from": {"id": 42}})
    await chat.observe(request)
    await chat.observe(request)
    await chat.observe(message(3, "我们打算周六上午去", 43))
    await due(chat)
    assert await chat.run_one()
    assert not await chat.run_one()
    assert len(model.calls) == 1
    context = model.calls[0][1]
    assert context["reply_to"] == 2
    assert [row["id"] for row in context["messages"]] == [1, 2, 3]
    assert context["messages"][0]["name"] == "小明"
    assert context["messages"][1]["reply_to"] == 1
    sent = [kwargs for method, kwargs in tg.calls if method == "send_message"]
    assert len(sent) == 1 and sent[0]["reply_parameters"]["message_id"] == 2
    async with db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(ChatTurn).where(ChatTurn.state == "done")) == 1
        assert await session.scalar(select(func.count()).select_from(ChatMessage).where(ChatMessage.is_bot)) == 1
        assert await session.scalar(select(func.count()).select_from(ArchivedMessage)) == 1


async def test_topics_and_groups_have_separate_context_and_telegram_targets(env):
    chat, model = await configured(env)
    _, db, tg, _ = env
    async with db.transaction() as session:
        session.add(Group(approved=True, id=-1002, title="另一个群", settings={**default_settings(), "chat_enabled": True}))
    await chat.observe(message(1, "柴郡喵聊聊爬山", message_thread_id=10, is_topic_message=True))
    await chat.observe(message(2, "柴郡喵聊聊电影", message_thread_id=20, is_topic_message=True))
    await chat.observe(message(3, "柴郡喵聊聊音乐", chat={"id": -1002, "type": "supergroup"}))
    await due(chat)
    for _ in range(3):
        assert await chat.run_one()
    assert [[row["id"] for row in call[1]["messages"]] for call in model.calls] == [[1], [2], [3]]
    sent = [data for method, data in tg.calls if method == "send_message"]
    assert [data.get("message_thread_id") for data in sent] == [10, 20, None]
    assert sent[-1]["chat_id"] == -1002


async def test_model_can_stay_silent_and_ambient_checks_have_cooldown(env):
    chat, model = await configured(env, chat_interval=90)
    model.result = None
    await chat.observe(message(1, "今天有点忙，晚点再看"))
    await due(chat)
    assert await chat.run_one()
    await chat.observe(message(2, "我也准备先去吃饭了"))
    await due(chat)
    assert not await chat.run_one()
    assert len(model.calls) == 1
    assert not any(method == "send_message" for method, _ in env[2].calls)
    async with chat.db.sessions() as session:
        assert await session.scalar(select(ChatTurn.state)) == "silent"


async def test_natural_followup_works_with_proactive_disabled(env):
    chat, model = await configured(env, chat_proactive=False)
    await chat.observe(message(1, "柴郡喵，你觉得周末去哪玩"))
    await due(chat)
    await chat.run_one()
    await age_reply(chat)
    await chat.observe(message(2, "那你觉得几点出发合适"))
    await chat.observe(message(3, "其他群友在聊另一件事", 43))
    await due(chat)
    assert await chat.run_one()
    assert len(model.calls) == 2 and model.calls[-1][1]["reply_to"] == 2
    assert any(row["bot"] for row in model.calls[-1][1]["messages"])
    assert not await chat.run_one()


async def test_runtime_filters_pending_members_ads_commands_bots_and_edits(env):
    chat, model = await configured(env)
    settings, db, tg, service = env
    runtime = Runtime(db, tg, service)
    await runtime.chat.close()
    runtime.chat = runtime.management.chat = chat
    async with db.transaction() as session:
        session.add(Rule(group_id=-1001, kind="keyword", value="广告"))
    await service.member_event(join(42))
    await deliver(runtime, {"message": message(1, "柴郡喵你在吗", 42)})
    await deliver(runtime, {"message": message(2, "柴郡喵广告看这里", 43)})
    await deliver(runtime, {"message": message(3, "/othercommand", 43)})
    bot_message = message(4, "柴郡喵你好呀", 44)
    bot_message["from"].update(is_bot=True, first_name="另一个bot")
    # deliver() intentionally rewrites the sender as a human, so use observe directly here.
    await chat.observe(bot_message)
    await deliver(runtime, {"message": message(5, "柴郡喵聊点什么", 43)})
    await deliver(runtime, {"edited_message": message(5, "柴郡喵广告改过来了", 43, edit_date=int(now()) + 1)})
    await due(chat)
    assert not await chat.run_one()
    assert not model.calls
    async with db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(ChatMessage).where(ChatMessage.eligible)) == 0


async def test_slow_model_does_not_hold_database_or_block_other_groups(env):
    chat, model = await configured(env)
    model.release = asyncio.Event()
    await chat.observe(message(1, "柴郡喵你怎么看"))
    await due(chat)
    running = asyncio.create_task(chat.run_one())
    await asyncio.wait_for(model.started.wait(), 2)
    try:
        # A different operation can acquire the writer while the model is still waiting.
        async with asyncio.timeout(2):
            async with chat.db.transaction() as session:
                group = await session.get(Group, -1001)
                group.settings = {**group.settings, "chat_enabled": False}
        model.release.set()
        await running
        assert not any(method == "send_message" for method, _ in env[2].calls)
    finally:
        model.release.set()
        await running


async def test_retry_restart_expiry_and_uncertain_send_are_handled_without_duplicates(env):
    chat, model = await configured(env)
    model.result = ChatModelError("API 暂不可用", retryable=True)
    await chat.observe(message(1, "柴郡喵聊聊周末"))
    await due(chat)
    await chat.run_one()
    async with chat.db.sessions() as session:
        turn = await session.scalar(select(ChatTurn))
        assert turn.state == "pending" and turn.attempts == 1
    model.result = "周末我投爬山一票。"
    await due(chat)
    env[2].fail["send_message"] = 1
    await chat.run_one()
    await chat.recover()
    assert not await chat.run_one()
    async with chat.db.transaction() as session:
        turn = await session.scalar(select(ChatTurn))
        assert turn.state == "failed" and turn.response
        turn.state = "sending"
    await chat.recover()
    async with chat.db.sessions() as session:
        turn = await session.scalar(select(ChatTurn))
        assert turn.state == "failed" and "未确认" in turn.error
    await chat.observe(message(2, "柴郡喵这是迟到的消息", date=int(now()) - 300))
    assert not await chat.run_one()


async def test_restart_restores_pending_context_and_reset_hides_old_messages(env):
    chat, model = await configured(env)
    _, db, tg, service = env
    await chat.observe(message(1, "柴郡喵先聊爬山"))
    async with db.transaction() as session:
        turn = await session.scalar(select(ChatTurn))
        turn.state = "generating"
    restored = GroupChat(db, tg, env[0], model=model)
    await restored.recover()
    await due(restored)
    assert await restored.run_one()
    runtime = Runtime(db, tg, service)
    await runtime.management.reset_chat(1, -1001, "reset-test")
    await age_reply(chat)
    await chat.observe(message(2, "柴郡喵换个话题聊电影"))
    await due(chat)
    assert await chat.run_one()
    assert [row["id"] for row in model.calls[-1][1]["messages"]] == [2]


async def test_bot_settings_and_http_status_use_existing_admin_scope(env, client):
    _, db, tg, service = env
    runtime = Runtime(db, tg, service)
    await open_group(runtime, tg)
    await click(runtime, tg, "群聊天设置")
    assert "尚未配置" in markup(tg)["text"]
    await click(runtime, tg, "已关闭 · 群聊天")
    await click(runtime, tg, "聊天昵称")
    await deliver(runtime, {"message": private("小橘")})
    async with db.sessions() as session:
        group = await session.get(Group, -1001)
        assert group.settings["chat_enabled"] and group.settings["chat_name"] == "小橘"
    status = await client.get("/api/groups/-1001/chat")
    assert status.status_code == 200 and status.json()["configured"] is False
    assert "api_key" not in status.text
    assert (await client.post("/api/groups/-1001/chat/reset")).status_code == 200
    assert (await client.get("/api/groups/-9999/chat")).status_code == 404


async def test_rolling_context_is_bounded_and_works_when_archive_disabled(env):
    chat, _ = await configured(env, archive_enabled=False, chat_proactive=False)
    for identifier in range(1, 90):
        await chat.observe(message(identifier, "群友今天随便聊了几句"))
    async with chat.db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(ChatMessage)) == 80
        assert await session.scalar(select(func.count()).select_from(ArchivedMessage)) == 0


@pytest.mark.parametrize("text,expected", [("@purrai_bot 你觉得呢", True), ("@purrai_bot你觉得呢", True), ("@purrai_bot_other hello", False), ("柴郡喵你在吗", True)])
def test_mention_detection(env, text, expected):
    tg = env[2]
    tg.username = "purrai_bot"
    assert addressed(message(1, text), tg, "柴郡喵") == expected


async def test_model_http_contract_and_vendor_extra_options():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": '```json\n{"should_reply":true,"reply":"明天见！"}\n```'}}]})

    settings = Settings(_env_file=None, chat_api_base="https://model.test/v1", chat_api_key="secret",
                        chat_model="example-chat", chat_api_extra={"enable_thinking": False})
    model = ChatModel(settings, httpx.MockTransport(handler))
    try:
        assert await model.reply("系统", "上下文") == ChatReply("明天见！")
        assert str(calls[0].url) == "https://model.test/v1/chat/completions"
        body = json.loads(calls[0].content)
        assert body["enable_thinking"] is False and body["max_tokens"] == 256
        assert "thinking" not in body and "response_format" not in body
        assert body["messages"][1]["content"] == "上下文" and not body["stream"]
    finally:
        await model.close()


async def test_default_deepseek_request_disables_thinking_and_uses_small_json_reply():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"should_reply":true,"reply":"明天见！"}'}}]})

    settings = Settings(_env_file=None, chat_api_key="test-secret")
    model = ChatModel(settings, httpx.MockTransport(handler))
    try:
        assert await model.reply('只输出 JSON：{"reply":"短回复"}', "上下文") == ChatReply("明天见！")
        assert str(calls[0].url) == "https://api.deepseek.com/chat/completions"
        body = json.loads(calls[0].content)
        assert body["model"] == "deepseek-flash" and body["max_tokens"] == 256
        assert body["thinking"] == {"type": "disabled"}
        assert body["response_format"] == {"type": "json_object"}
    finally:
        await model.close()


async def test_late_original_does_not_replace_an_edit_and_quotes_survive_short_history(env):
    chat, model = await configured(env, chat_proactive=False, chat_history=10)
    await chat.observe(message(900, "周末爬山的老话题", date=int(now()) - 60))
    for identifier in range(10, 20):
        await chat.observe(message(identifier, "接着聊最近的话题", date=int(now()) - 30))
    original = message(1, "柴郡喵这事你怎么看", reply_to_message={"message_id": 900, "text": "周末爬山的老话题"})
    await chat.observe(original, update_id=1)
    await chat.observe({**original, "text": "柴郡喵我改问电影了", "edit_date": int(now()) + 1}, edited=True, update_id=2)
    await chat.observe(original, update_id=1)
    await due(chat)
    assert await chat.run_one()
    context = model.calls[0][1]["messages"]
    edited = next(row for row in context if row["id"] == 1)
    assert edited["text"] == "柴郡喵我改问电影了"
    assert edited["quote"] == "周末爬山的老话题"


async def test_telegram_rate_limit_reuses_generated_reply_instead_of_calling_model_again(env):
    from aiogram.exceptions import TelegramRetryAfter
    from aiogram.methods import SendMessage

    chat, model = await configured(env)
    tg = env[2]
    original_call = tg.call
    limited = True

    async def call(method, **kwargs):
        nonlocal limited
        if method == "send_message" and limited:
            limited = False
            raise TelegramRetryAfter(method=SendMessage(chat_id=-1001, text="test"), message="wait", retry_after=1)
        return await original_call(method, **kwargs)

    tg.call = call
    await chat.observe(message(1, "柴郡喵聊聊周末"))
    await due(chat)
    assert await chat.run_one()
    await due(chat)
    assert await chat.run_one()
    assert len(model.calls) == 1
    async with chat.db.sessions() as session:
        assert await session.scalar(select(ChatTurn.state)) == "done"


@pytest.mark.parametrize("code,payload,retryable", [
    (401, {"error": "secret-key-is-in-provider-body"}, False),
    (429, {}, True), (503, {}, True),
    (200, {"choices": [{"message": {"content": "普通文字，没有JSON"}}]}, False),
])
async def test_model_errors_are_safe_and_classified(code, payload, retryable):
    settings = Settings(_env_file=None, chat_api_base="https://model.test/chat/completions", chat_api_key="secret", chat_model="test")
    model = ChatModel(settings, httpx.MockTransport(lambda request: httpx.Response(code, json=payload)))
    try:
        with pytest.raises(ChatModelError) as error:
            await model.reply("system", "context")
        assert error.value.retryable is retryable
        assert "secret" not in str(error.value)
    finally:
        await model.close()
