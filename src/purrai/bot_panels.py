"""Navigation facade over independently scoped Telegram panels."""

from purrai.bot_archive_panels import ArchivePanels
from purrai.bot_configuration_panels import ConfigurationPanels
from purrai.bot_group_panels import GroupPanels
from purrai.bot_record_panels import RecordPanels
from purrai.bot_system_panels import SystemPanels


class BotPanels:
    def __init__(self, management, ui, state):
        self.group = GroupPanels(management, ui, state)
        self.configuration = ConfigurationPanels(management, ui, state)
        self.archive = ArchivePanels(management, ui, state)
        self.record = RecordPanels(management, ui, state)
        self.system_views = SystemPanels(management, ui, state)

    async def choose_groups(self, actor, page=0, message_id=None):
        return await self.group.choose_groups(actor, page, message_id)

    async def group_panel(self, actor, group_id, message_id=None):
        return await self.group.group_panel(actor, group_id, message_id)

    async def access_panel(self, actor, group_id, message_id=None):
        return await self.group.access_panel(actor, group_id, message_id)

    async def admins(self, actor, message_id=None, page=0):
        return await self.group.admins(actor, message_id, page)

    async def chat_panel(self, actor, group_id, message_id=None):
        return await self.configuration.chat_panel(actor, group_id, message_id)

    async def settings_panel(self, actor, group_id, section, message_id=None):
        return await self.configuration.settings_panel(actor, group_id, section, message_id)

    async def emoji_packs_panel(self, actor, group_id, page=0, message_id=None):
        return await self.configuration.emoji_packs_panel(actor, group_id, page, message_id)

    async def archive_panel(self, actor, group_id, filters=None, page=0, message_id=None):
        return await self.archive.archive_panel(actor, group_id, filters, page, message_id)

    async def archive_detail(self, actor, group_id, data, message_id=None):
        return await self.archive.archive_detail(actor, group_id, data, message_id)

    async def list_panel(self, actor, group_id, kind, page=0, message_id=None):
        return await self.record.list_panel(actor, group_id, kind, page, message_id)

    async def detail(self, actor, group_id, kind, identifier, message_id=None):
        return await self.record.detail(actor, group_id, kind, identifier, message_id)

    async def system(self, actor, message_id=None):
        return await self.system_views.system(actor, message_id)

    async def failures(self, actor, event=False, page=0, message_id=None):
        return await self.system_views.failures(actor, event, page, message_id)
