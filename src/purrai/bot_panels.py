"""Navigation facade over independently scoped Telegram panels."""

from purrai.bot_archive_panels import ArchivePanels
from purrai.bot_configuration_panels import ConfigurationPanels
from purrai.bot_group_panels import GroupPanels
from purrai.bot_record_panels import RecordPanels
from purrai.bot_rule_panels import RulePanels
from purrai.bot_system_panels import SystemPanels
from purrai.bot_policy_panels import PolicyPanels
from purrai.bot_review_panels import ReviewPanels
from purrai.bot_member_panels import MemberPanels
from purrai.bot_batch_panels import BatchPanels
from purrai.bot_operations_panels import OperationsPanels
from purrai.bot_auto_reply_panels import AutoReplyPanels
from purrai.bot_lottery_panels import LotteryPanels


class BotPanels:
    def __init__(self, management, ui, state):
        self.group = GroupPanels(management, ui, state)
        self.configuration = ConfigurationPanels(management, ui, state)
        self.archive = ArchivePanels(management, ui, state)
        self.record = RecordPanels(management, ui, state)
        self.rules = RulePanels(management, ui, self.record)
        self.system_views = SystemPanels(management, ui, state)
        self.policy = PolicyPanels(management, ui, state)
        self.review = ReviewPanels(management, ui, state)
        self.members = MemberPanels(management, ui)
        self.batch = BatchPanels(management, ui, state)
        self.operations = OperationsPanels(management, ui, state)
        self.auto_replies = AutoReplyPanels(management, ui, state)
        self.lottery = LotteryPanels(management, ui, state)
        self.action_handlers, self.input_handlers = {}, {}
        domains = (
            (self.group, True),
            (self.configuration, True),
            (self.archive, False),
            (self.record, True),
            (self.rules, True),
            (self.members, True),
            (self.system_views, False),
        )
        for panel, clear_prompt in domains:
            self.register(self.action_handlers, panel.ACTIONS, panel.route)
            kinds = getattr(panel, "INPUTS", ())
            if kinds:
                self.register(self.input_handlers, kinds, (panel.input, clear_prompt))
        self.register(self.input_handlers, ("lottery",), (self.lottery.input, False))
        self.register(self.input_handlers, ("auto_reply",), (self.auto_replies.input, False))
        self.register(self.input_handlers, ("batch_reason",), (self.batch.input, False))
        self.action_prefixes = (
            ("lottery_", self.lottery.route, False),
            ("autoreply_", self.auto_replies.route, False),
            ("ops_", self.operations.route, False),
            ("batch_", self.batch.route, False),
            ("review_", self.review.route, True),
            ("policy_", self.policy.route, True),
        )
        self.input_prefixes = (
            ("ops_", self.operations.input, False),
            ("review_", self.review.input, False),
            ("policy_", self.policy.input, False),
            ("archive_", self.archive.input, False),
        )

    @staticmethod
    def register(handlers, names, handler):
        names = tuple(names)
        overlap = handlers.keys() & set(names)
        duplicates = overlap or {name for name in names if names.count(name) > 1}
        if duplicates:
            raise ValueError(f"Duplicate Telegram handlers: {sorted(duplicates)}")
        handlers.update(dict.fromkeys(names, handler))

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
