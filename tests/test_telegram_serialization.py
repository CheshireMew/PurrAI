import json
from types import SimpleNamespace

from aiogram.types import Update
from sqlalchemy import select

from conftest import message
from purrai.models import ArchivedMessage, Inbox
from purrai.runtime import Runtime
from purrai.telegram import TelegramGateway, telegram_data


def update_with_link_preview(identifier=801):
    payload = message(81, "带链接预览的消息 https://example.com")
    payload["from"].update(is_bot=False, first_name="测试成员")
    payload["link_preview_options"] = {"url": "https://example.com", "is_disabled": False}
    payload["entities"] = [{"type": "url", "offset": 9, "length": 19}]
    return Update.model_validate({"update_id": identifier, "message": payload})


def test_sdk_defaults_are_omitted_but_explicit_false_and_api_aliases_remain():
    data = telegram_data([update_with_link_preview()])
    assert json.loads(json.dumps(data)) == data
    payload = data[0]["message"]
    assert payload["from"]["id"] == 42 and "from_user" not in payload
    assert payload["link_preview_options"] == {"url": "https://example.com", "is_disabled": False}
    assert isinstance(payload["date"], int)


async def test_real_sdk_update_persists_routes_and_archives_with_link_preview(env):
    _, database, telegram, service = env
    runtime = Runtime(database, telegram, service)
    await runtime.persist(telegram_data([update_with_link_preview()]))
    assert await runtime.process_one()
    async with database.sessions() as session:
        item = await session.get(Inbox, 801)
        assert item.state == "done", item.error
        row = (await session.scalars(select(ArchivedMessage))).one()
        assert row.text.startswith("带链接预览的消息")


async def test_gateway_converts_both_single_responses_and_update_lists():
    gateway = TelegramGateway("")
    update = update_with_link_preview()

    async def receive(**kwargs):
        return [update]

    async def send(**kwargs):
        return update.message

    gateway.bot = SimpleNamespace(get_updates=receive, send_message=send)
    updates = await gateway.call("get_updates", timeout=0)
    assert updates[0]["message"]["link_preview_options"]["is_disabled"] is False
    result = await gateway.call("send_message", chat_id=-1001, text="测试")
    assert result["message_id"] == 81 and result["from"]["id"] == 42
