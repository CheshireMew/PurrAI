"""Administration facade shared by Telegram, HTTP and the local CLI."""

from purrai.admin_context import AdminContext
from purrai.archive_admin import ArchiveAdministration
from purrai.chat_admin import ChatAdministration
from purrai.errors import ManagementError as ManagementError
from purrai.group_access import GroupAccess
from purrai.group_admin import GroupAdministration
from purrai.group_configuration import GroupConfiguration
from purrai.management_queries import ManagementQueries
from purrai.member_admin import MemberAdministration
from purrai.rule_admin import RuleAdministration
from purrai.system_admin import SystemAdministration


class Management:
    def __init__(self, db, telegram, service=None, settings=None, *, chat=None, access=None):
        # The service argument preserves the existing Python API; operations do not retain it.
        settings = settings if settings is not None else service.settings
        self.db, self.tg, self.settings = db, telegram, settings
        self.access = access or GroupAccess(db, telegram, settings)
        context = AdminContext(db, telegram, settings, self.access)
        self.groups_admin = GroupAdministration(context)
        self.configuration = GroupConfiguration(context)
        self.members = MemberAdministration(context)
        self.rules = RuleAdministration(context)
        self.archives = ArchiveAdministration(context)
        self.system_admin = SystemAdministration(context)
        self.queries = ManagementQueries(context)
        self.chat_admin = ChatAdministration(context, chat)

    @property
    def chat(self):
        return self.chat_admin.chat

    @chat.setter
    def chat(self, value):
        self.chat_admin.chat = value

    @staticmethod
    async def current_group(session, group_id):
        return await GroupAccess.current_group(session, group_id)

    def group_transaction(self, group_id):
        return self.access.group_transaction(group_id)

    async def principal(self, actor: int, root=False):
        return await self.access.principal(actor, root)

    async def authorize(self, actor: int, group_id: int, write=False, root=False):
        return await self.access.authorize(actor, group_id, write, root)

    async def groups(self, actor):
        return await self.access.groups(actor)

    async def require_join_right(self, group_id):
        return await self.access.require_join_right(group_id)

    async def group_access(self, actor, group_id, allow):
        return await self.groups_admin.group_access(actor, group_id, allow)

    async def admit_group(self, actor, group_id):
        return await self.groups_admin.admit_group(actor, group_id)

    async def set_group_access(self, group_id, allow, actor=None):
        return await self.groups_admin.set_group_access(group_id, allow, actor)

    async def permissions(self, actor, group_id, activate=False):
        return await self.groups_admin.permissions(actor, group_id, activate)

    async def join_link(self, actor, group_id, request_id):
        return await self.groups_admin.join_link(actor, group_id, request_id)

    async def settings_patch(self, actor, group_id, body, request_id=""):
        return await self.configuration.settings_patch(actor, group_id, body, request_id)

    async def setting(self, actor, group_id, key, value, request_id):
        return await self.configuration.setting(actor, group_id, key, value, request_id)

    async def change_status_packs(self, actor, group_id, body):
        return await self.configuration.change_status_packs(actor, group_id, body)

    @staticmethod
    def status_pack_replay(previous, group_id, body):
        return GroupConfiguration.status_pack_replay(previous, group_id, body)

    async def validate_status_packs(self, packs):
        return await self.configuration.validate_status_packs(packs)

    async def verification_action(self, actor, identifier, body):
        return await self.members.verification_action(actor, identifier, body)

    async def release_pending(self, actor, group_id, body):
        return await self.members.release_pending(actor, group_id, body)

    async def violation_action(self, actor, group_id, identifier, body):
        return await self.members.violation_action(actor, group_id, identifier, body)

    async def member_action(self, actor, group_id, user_id, action, request_id, reason="Telegram 管理员操作"):
        return await self.members.member_action(actor, group_id, user_id, action, request_id, reason)

    async def rule(self, actor, group_id, body, identifier="", request_id=""):
        return await self.rules.rule(actor, group_id, body, identifier, request_id)

    async def preview(self, actor, group_id, body):
        return await self.rules.preview(actor, group_id, body)

    async def whitelist(self, actor, group_id, body, request_id="", remove=False):
        return await self.rules.whitelist(actor, group_id, body, request_id, remove)

    async def admin(self, actor, body, request_id=""):
        return await self.rules.admin(actor, body, request_id)

    async def archived_messages(self, actor, group_id, filters, page_number=1, size=20):
        return await self.archives.archived_messages(actor, group_id, filters, page_number, size)

    async def archived_message(self, actor, group_id, identifier):
        return await self.archives.archived_message(actor, group_id, identifier)

    async def archive_export(self, actor, group_id, filters, request_id):
        return await self.archives.archive_export(actor, group_id, filters, request_id)

    async def track(self, actor, group_id, chat_id, action_ids, request_id):
        return await self.system_admin.track(actor, group_id, chat_id, action_ids, request_id)

    async def backup(self, actor, request_id=""):
        return await self.system_admin.backup(actor, request_id)

    async def retry(self, actor, identifier, event=False, request_id=""):
        return await self.system_admin.retry(actor, identifier, event, request_id)

    async def records(self, actor, group_id, model, page=0, pending=False):
        return await self.queries.records(actor, group_id, model, page, pending)

    async def chat_status(self, actor, group_id):
        return await self.chat_admin.chat_status(actor, group_id)

    async def reset_chat(self, actor, group_id, request_id):
        return await self.chat_admin.reset_chat(actor, group_id, request_id)
