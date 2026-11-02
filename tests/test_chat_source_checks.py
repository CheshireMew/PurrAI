import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.methods import EditMessageReplyMarkup
from sqlalchemy import select

from conftest import message
from purrai import chat_source_checks
from purrai.chat_source_checks import ReplySourceChecks
from purrai.models import Action, ChatTurn, now
from test_chat import configured
from test_chat_retraction import cleanup


def source_response(chat, outcome):
    original = chat.tg.call

    async def call(method, **kwargs):
        if method == "get_chat_member" and outcome == "no_access":
            return {"status": "left"}
        result = await original(method, **kwargs)
        if method == "edit_message_reply_markup" and outcome == "control_absent" and kwargs["message_id"] != 1:
            raise TelegramBadRequest(method=EditMessageReplyMarkup(chat_id=kwargs["chat_id"], message_id=kwargs["message_id"]),
                                     message="message to edit not found")
        if method == "edit_message_reply_markup" and kwargs["message_id"] == 1:
            api = EditMessageReplyMarkup(chat_id=kwargs["chat_id"], message_id=1)
            if outcome == "absent":
                raise TelegramBadRequest(method=api, message="message to edit not found")
            if outcome == "network":
                raise ConnectionError("offline")
            if outcome == "rate_limit":
                raise TelegramRetryAfter(method=api, message="wait", retry_after=120)
            if outcome in ("cant_edit", "not_modified", "unknown_error"):
                error = {"cant_edit": "message can't be edited", "not_modified": "message is not modified",
                         "unknown_error": "chat not found"}[outcome]
                raise TelegramBadRequest(method=api, message=error)
        return result
    chat.tg.call = call


@pytest.mark.parametrize("archive_enabled", [False, True])
async def test_external_deletion_requires_two_checks_and_retracts_without_model(env, monkeypatch, archive_enabled):
    chat, model = await configured(env, archive_enabled=archive_enabled)
    await chat.observe(message(1, "柴郡喵，周末去哪里走走？"))
    assert await chat.run_one()
    source_response(chat, "absent")
    instant = [now()]
    monkeypatch.setattr(chat_source_checks, "now", lambda: instant[0])
    checker = ReplySourceChecks(chat.db, chat.tg)
    assert await checker.sweep() == 0
    assert not any(method == "delete_message" for method, _ in chat.tg.calls)
    instant[0] += 31
    assert await ReplySourceChecks(chat.db, chat.tg).sweep() == 1
    await cleanup(env[3])
    assert [kw["message_id"] for method, kw in chat.tg.calls if method == "delete_message"] == [101]
    assert len(model.calls) == 1
    async with chat.db.sessions() as session:
        assert await session.scalar(select(ChatTurn.state)) == "retracted"
        assert len((await session.scalars(select(Action).where(Action.dedupe_key.startswith("confirmed-absent:")))).all()) == 1


@pytest.mark.parametrize("outcome", ["present", "cant_edit", "not_modified", "unknown_error", "network", "rate_limit", "no_access", "control_absent"])
async def test_inconclusive_or_present_source_never_triggers_deletion(env, monkeypatch, outcome):
    chat, model = await configured(env)
    await chat.observe(message(1, "柴郡喵，今晚吃点什么？"))
    assert await chat.run_one()
    source_response(chat, outcome)
    instant = [now()]
    monkeypatch.setattr(chat_source_checks, "now", lambda: instant[0])
    checker = ReplySourceChecks(chat.db, chat.tg)
    assert await checker.sweep() == 0
    instant[0] += 31
    assert await checker.sweep() == 0
    assert not any(method == "delete_message" for method, _ in chat.tg.calls)
    assert len(model.calls) == 1
