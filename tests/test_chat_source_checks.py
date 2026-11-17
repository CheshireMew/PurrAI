import asyncio
import json
import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.methods import EditMessageReplyMarkup
from sqlalchemy import select

from conftest import message
from purrai import chat_source_checks
from purrai.chat_source_checks import ReplySourceChecks
from purrai.models import Action, ChatTurn, Meta, now
from purrai.telegram_health import request_purpose
from test_chat import configured
from test_chat_retraction import cleanup


@pytest.fixture(autouse=True)
def fast_requests(monkeypatch):
    monkeypatch.setattr(chat_source_checks, "REQUEST_SPACING", 0)


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


async def test_network_failure_retries_only_failed_record_and_survives_restart(env, monkeypatch):
    chat, _ = await configured(env)
    for identifier in (1, 2):
        await chat.observe(message(identifier, f"柴郡喵，帮我规划一下第{identifier}天的行程？", user_id=42 + identifier))
        assert await chat.run_one()
    instant = [now()]
    monkeypatch.setattr(chat_source_checks, "now", lambda: instant[0])
    chat.tg.fail["get_chat_member"] = 1
    checker = ReplySourceChecks(chat.db, chat.tg)
    before = len(chat.tg.calls)
    assert await checker.sweep() == 0
    assert len(chat.tg.calls) == before + 4  # A different record still completes.
    assert checker.status()["delayed"] == 1
    assert not checker.state.get("pause_until")
    assert {kwargs["request_timeout"] for _, kwargs in chat.tg.calls[before:]} == {10}
    before = len(chat.tg.calls)
    instant[0] += 29
    assert await ReplySourceChecks(chat.db, chat.tg).sweep() == 0
    assert len(chat.tg.calls) == before
    instant[0] += 2
    assert await checker.sweep() == 0
    assert len(chat.tg.calls) > before
    assert checker.status()["delayed"] == 0
    assert not any(method == "delete_message" for method, _ in chat.tg.calls)


async def test_persisted_backoff_grows_to_cap_and_success_resets_it(env, monkeypatch):
    chat, _ = await configured(env)
    await chat.observe(message(1, "柴郡喵，帮我安排周末？"))
    assert await chat.run_one()
    original = chat.tg.call
    source_response(chat, "network")
    instant = [now()]
    monkeypatch.setattr(chat_source_checks, "now", lambda: instant[0])
    first = instant[0]
    for failure, delay in enumerate((30, 60, 120, 240, 480, 900, 900), 1):
        checker = ReplySourceChecks(chat.db, chat.tg)
        await checker.sweep()
        entry = next(iter(checker.state["items"].values()))
        assert entry["failures"] == failure and entry["next"] == instant[0] + delay
        assert checker.status()["first_failure"] == first
        before = len(chat.tg.calls)
        instant[0] += delay - 1
        await checker.sweep()
        assert len(chat.tg.calls) == before
        instant[0] += 1
    chat.tg.call = original
    await checker.sweep()
    assert checker.status()["delayed"] == 0
    assert next(iter(checker.state["items"].values()))["next"] == instant[0] + 300


async def test_rate_limit_pauses_auxiliary_queue_but_not_other_telegram_calls(env, monkeypatch):
    chat, _ = await configured(env)
    for identifier in (1, 2):
        await chat.observe(message(identifier, "柴郡喵，帮我计划旅行？", user_id=42 + identifier))
        assert await chat.run_one()
    source_response(chat, "rate_limit")
    instant = [now()]
    monkeypatch.setattr(chat_source_checks, "now", lambda: instant[0])
    checker = ReplySourceChecks(chat.db, chat.tg)
    await checker.sweep()
    assert checker.state["pause_until"] == instant[0] + 120
    assert len(checker.state["items"]) == 1
    await chat.tg.call("get_updates")
    before = len(chat.tg.calls)
    instant[0] += 119
    await ReplySourceChecks(chat.db, chat.tg).sweep()
    assert len(chat.tg.calls) == before
    instant[0] += 1
    await checker.sweep()
    assert len(chat.tg.calls) > before
    assert not any(method == "delete_message" for method, _ in chat.tg.calls)


async def test_uncertainty_resets_absence_confirmation_and_stale_warning_is_pruned(env, monkeypatch):
    chat, _ = await configured(env)
    await chat.observe(message(1, "柴郡喵，周末如何安排？"))
    assert await chat.run_one()
    original = chat.tg.call
    instant = [now()]
    monkeypatch.setattr(chat_source_checks, "now", lambda: instant[0])
    for outcome in ("absent", "network", "absent"):
        chat.tg.call = original
        source_response(chat, outcome)
        assert await ReplySourceChecks(chat.db, chat.tg).sweep() == 0
        instant[0] += 31
    checker = ReplySourceChecks(chat.db, chat.tg)
    assert await checker.sweep() == 1
    assert checker.status()["delayed"] == 0
    assert not checker.state["items"]
    # An expired/withdrawn turn must not leave a permanent warning after restart.
    async with chat.db.transaction() as session:
        row = await session.get(Meta, chat_source_checks.KEY)
        row.value = json.dumps({"items": {"unknown-turn": {"failures": 2, "first_failure": 1}}})
    await checker.restore()
    assert checker.status()["delayed"] == 0


async def test_source_request_scope_pacing_and_cancellation_do_not_leak(env, monkeypatch):
    chat, _ = await configured(env)
    checker = ReplySourceChecks(chat.db, chat.tg)
    seen, started = [], asyncio.Event()
    monkeypatch.setattr(chat_source_checks, "REQUEST_SPACING", 0.02)

    async def call(method, **kwargs):
        seen.append((request_purpose.get(), asyncio.get_running_loop().time()))
        if method == "blocked":
            started.set()
            await asyncio.Future()
        return True

    monkeypatch.setattr(chat.tg, "call", call)
    await checker.call("first")
    task = asyncio.create_task(checker.call("blocked"))
    await started.wait()
    assert request_purpose.get() == "operations"
    await asyncio.wait_for(chat.tg.call("get_updates"), 0.1)
    assert seen[2][0] == "operations"  # Core traffic continues during a stuck probe.
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert seen[0][0] == seen[1][0] == "source_check"
    assert seen[1][1] - seen[0][1] >= 0.015
    assert request_purpose.get() == "operations"
