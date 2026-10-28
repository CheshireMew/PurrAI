import pytest
from aiogram.types import InlineKeyboardMarkup

from purrai.runtime import Runtime
from test_bot_admin import click, deliver, markup, private


@pytest.mark.parametrize("public_url", ["https://tgbot.blacknico.com", "https://tgbot.blacknico.com/"])
async def test_web_login_is_a_full_width_link_and_preserves_group_navigation(env, public_url):
    settings, database, telegram, service = env
    settings.public_url = public_url
    runtime = Runtime(database, telegram, service)
    await deliver(runtime, {"message": private("/admin")})
    rows = markup(telegram)["reply_markup"]["inline_keyboard"]
    assert rows[0] == [{"text": "🌐 登录网页后台", "login_url": {"url": "https://tgbot.blacknico.com/auth/telegram/button"}, "style": "primary"}]
    InlineKeyboardMarkup(inline_keyboard=rows)
    await click(runtime, telegram, "测试群")
    assert "验证设置" in str(markup(telegram)["reply_markup"])
    await click(runtime, telegram, "切换群")
    refreshed = markup(telegram)["reply_markup"]["inline_keyboard"]
    assert refreshed[0] == rows[0]


async def test_local_web_address_is_not_offered_as_a_public_login_link(env):
    _, database, telegram, service = env
    runtime = Runtime(database, telegram, service)
    await deliver(runtime, {"message": private("/admin")})
    assert all("url" not in item and "login_url" not in item
               for row in markup(telegram)["reply_markup"]["inline_keyboard"] for item in row)


async def test_refreshing_panel_with_web_link_keeps_existing_callback_tokens(env):
    settings, database, telegram, service = env
    settings.public_url = "https://tgbot.blacknico.com"
    runtime = Runtime(database, telegram, service)
    await runtime.admin.panels.choose_groups(1)
    first = markup(telegram)
    old = first["reply_markup"]["inline_keyboard"]
    await runtime.admin.panels.choose_groups(1, message_id=telegram.message_id)
    assert markup(telegram)["reply_markup"]["inline_keyboard"] == old
