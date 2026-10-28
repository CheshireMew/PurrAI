"""Stable Telegram administration entry points and dependency composition."""

from purrai.admin_labels import CHAT_FIELDS as CHAT_FIELDS
from purrai.bot_callbacks import BotCallbacks
from purrai.bot_commands import BotCommands
from purrai.bot_common import button as button
from purrai.bot_input import BotInput
from purrai.bot_panels import BotPanels
from purrai.bot_routes import BotRoutes
from purrai.bot_state import BotState
from purrai.bot_ui import BotUI


class BotAdmin:
    def __init__(self, management):
        self.access = management.access
        self.state = BotState(management.db)
        self.ui = BotUI(management.tg, management.settings, self.state)
        self.panels = BotPanels(management, self.ui, self.state)
        self.inputs = BotInput(management, self.ui, self.state, self.panels)
        self.routes = BotRoutes(management, self.ui, self.state, self.panels, self.inputs)
        self.commands = BotCommands(management, self.ui, self.state, self.panels, self.inputs)
        self.callbacks = BotCallbacks(self.ui, self.state, self.routes, self.inputs)

    async def message(self, message):
        async with self.access.operation():
            return await self.commands.message(message)

    async def callback(self, query):
        async with self.access.operation():
            return await self.callbacks.callback(query)

    async def remember(self, key, value):
        return await self.state.remember(key, value)

    async def recall(self, key):
        return await self.state.recall(key)

    async def panel(self, actor, text, buttons, group_id=None, message_id=None, *, resume=None):
        return await self.ui.panel(actor, text, buttons, group_id, message_id, resume=resume)

    async def send(self, chat_id, text):
        return await self.ui.send(chat_id, text)

    async def choose_groups(self, actor, page=0, message_id=None):
        return await self.panels.choose_groups(actor, page, message_id)

    async def group_panel(self, actor, group_id, message_id=None):
        return await self.panels.group_panel(actor, group_id, message_id)

    async def access_panel(self, actor, group_id, message_id=None):
        return await self.panels.access_panel(actor, group_id, message_id)

    async def admins(self, actor, message_id=None, page=0):
        return await self.panels.admins(actor, message_id, page)

    async def chat_panel(self, actor, group_id, message_id=None):
        return await self.panels.chat_panel(actor, group_id, message_id)

    async def settings_panel(self, actor, group_id, section, message_id=None):
        return await self.panels.settings_panel(actor, group_id, section, message_id)

    async def emoji_packs_panel(self, actor, group_id, page=0, message_id=None):
        return await self.panels.emoji_packs_panel(actor, group_id, page, message_id)

    async def archive_panel(self, actor, group_id, filters=None, page=0, message_id=None):
        return await self.panels.archive_panel(actor, group_id, filters, page, message_id)

    async def archive_detail(self, actor, group_id, data, message_id=None):
        return await self.panels.archive_detail(actor, group_id, data, message_id)

    async def list_panel(self, actor, group_id, kind, page=0, message_id=None):
        return await self.panels.list_panel(actor, group_id, kind, page, message_id)

    async def detail(self, actor, group_id, kind, identifier, message_id=None):
        return await self.panels.detail(actor, group_id, kind, identifier, message_id)

    async def system(self, actor, message_id=None):
        return await self.panels.system(actor, message_id)

    async def failures(self, actor, event=False, page=0, message_id=None):
        return await self.panels.failures(actor, event, page, message_id)
