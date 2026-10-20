import asyncio

import pytest
from sqlalchemy import select

from conftest import drain
from purrai import captcha
from purrai.models import Action, Verification, now, uid
from purrai.runtime import Runtime
from purrai.service import Service
from test_verification import fixed_challenge, latest


def choice_query(verification, *, code="123456", identifier=None):
    return {
        "id": identifier or uid(),
        "from": {"id": verification.user_id, "is_bot": False, "first_name": "成员"},
        "chat_instance": "choice-test",
        "message": {"message_id": verification.message_id, "date": int(now()),
                    "chat": {"id": verification.prompt_chat_id or verification.group_id,
                             "type": "supergroup"}},
        "data": f"v:{verification.id}:{verification.nonce}:c{verification.choices.index(code)}",
    }


def test_choices_are_unique_six_digits_and_include_leading_zero_answer():
    choices = captcha.make_choices("001122")
    assert len(choices) == len(set(choices)) == 6
    assert choices.count("001122") == 1
    assert all(len(code) == 6 and code.isascii() and code.isdecimal() for code in choices)
    rows = captcha.keyboard("a" * 32, "b" * 8, choices)["inline_keyboard"]
    assert [len(row) for row in rows] == [2, 2, 2]
    assert [button["text"] for row in rows for button in row] == choices
    assert max(len(button["callback_data"].encode()) for row in rows for button in row) <= 64


async def test_new_challenge_sends_six_complete_choices_and_no_progress(env, monkeypatch):
    verification = await fixed_challenge(env, monkeypatch)
    _, db, tg, _ = env
    prompt = next(args for method, args in tg.calls if method == "send_photo")
    assert prompt["reply_markup"] == captcha.keyboard(verification.id, verification.nonce, verification.choices)
    assert "只需选择一次" in prompt["caption"] and "已输入" not in prompt["caption"]
    assert "键盘" not in prompt["caption"] and "●" not in prompt["caption"]
    async with db.sessions() as session:
        assert not (await session.scalars(select(Action).where(Action.kind == "progress"))).all()


async def test_one_click_passes_and_concurrent_replays_only_queue_one_release(env, monkeypatch):
    verification = await fixed_challenge(env, monkeypatch)
    _, db, tg, service = env
    query = choice_query(verification)
    await asyncio.gather(*(service.callback(query) for _ in range(3)),
                         service.callback(choice_query(verification)))
    row = await latest(db)
    assert row.state == "releasing" and row.input_digits == "123456" and row.attempts == 0
    async with db.sessions() as session:
        assert len((await session.scalars(select(Action).where(Action.kind == "release"))).all()) == 1
        assert not (await session.scalars(select(Action).where(Action.kind == "progress"))).all()
    await drain(service)
    assert (await latest(db)).state == "passed"
    assert not any(method == "edit_message_caption" for method, _ in tg.calls)


async def test_wrong_choice_rotates_question_once_and_old_buttons_do_not_consume_attempts(env, monkeypatch):
    original = await fixed_challenge(env, monkeypatch)
    settings, db, tg, service = env
    wrong = next(code for code in original.choices if code != "123456")
    query = choice_query(original, code=wrong)
    await service.callback(query)
    await service.callback({**query, "id": uid()})
    changed = await latest(db)
    assert changed.attempts == 1 and changed.state == "muting"
    assert changed.nonce != original.nonce and changed.deadline == original.deadline
    restarted = Service(db, tg, settings)
    await drain(restarted)
    current = await latest(db)
    assert current.state == "pending" and len(current.choices) == 6
    assert current.choices.count("123456") == 1
    await restarted.callback(choice_query(original))
    assert (await latest(db)).attempts == 1
    await restarted.callback(choice_query(current))
    await drain(restarted)
    assert (await latest(db)).state == "passed"


@pytest.mark.parametrize("key", ["c6", "c-1", "c00", "cX", "c", "1", "back", "ok"])
async def test_only_offered_choice_indices_are_accepted(env, monkeypatch, key):
    verification = await fixed_challenge(env, monkeypatch)
    _, db, _, service = env
    query = choice_query(verification)
    query["data"] = f"v:{verification.id}:{verification.nonce}:{key}"
    assert "完整答案" in await service.callback(query)
    row = await latest(db)
    assert row.state == "pending" and row.input_digits == "" and row.attempts == 0


@pytest.mark.parametrize("invalid", ["other_user", "other_message", "expired"])
async def test_choice_still_checks_owner_message_and_deadline(env, monkeypatch, invalid):
    verification = await fixed_challenge(env, monkeypatch)
    _, db, _, service = env
    query = choice_query(verification)
    if invalid == "other_user":
        query["from"]["id"] = 43
    elif invalid == "other_message":
        query["message"]["message_id"] += 1
    else:
        async with db.transaction() as session:
            (await session.get(Verification, verification.id)).deadline = now() - 1
    await service.callback(query)
    row = await latest(db)
    assert row.state == ("failing" if invalid == "expired" else "pending")
    assert row.input_digits == "" and row.attempts == 0


async def test_choice_releases_before_slow_callback_ack_finishes(env, monkeypatch):
    verification = await fixed_challenge(env, monkeypatch)
    _, db, tg, service = env
    runtime = Runtime(db, tg, service)
    entered, finish = asyncio.Event(), asyncio.Event()
    original = tg.call

    async def slow(method, **kwargs):
        if method == "answer_callback_query":
            entered.set()
            await finish.wait()
        return await original(method, **kwargs)

    monkeypatch.setattr(tg, "call", slow)
    try:
        await runtime.persist([{"update_id": 1000, "callback_query": choice_query(verification)}])
        assert await asyncio.wait_for(runtime.process_one("verification"), 1)
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(service.run_one_action(include_progress=False), 1)
        assert (await latest(db)).state == "passed"
    finally:
        finish.set()
        await runtime.stop()
