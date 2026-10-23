"""Application composition and stable event/worker entry points."""

from purrai.action_queue import audit as audit
from purrai.action_queue import enqueue as enqueue
from purrai.actions import ActionRunner
from purrai.advertising_guard import AdvertisingGuard
from purrai.emoji_status import EmojiStatusGuard
from purrai.group_access import GroupAccess
from purrai.maintenance import Maintenance
from purrai.member_events import MemberEvents
from purrai.member_trust import MemberTrust
from purrai.moderation import Moderation
from purrai.restriction_state import owns_verification_ban, restriction_identity
from purrai.serialization import row_dict as row_dict
from purrai.verification import VerificationService
from purrai.verification_cleanup import VerificationCleanup, cleanup
from purrai.verification_progress import VerificationProgress
from purrai.verification_state import ACTIVE as ACTIVE


class Service:
    def __init__(self, db, telegram, settings):
        self.db, self.tg, self.settings = db, telegram, settings
        self.access = GroupAccess(db, telegram, settings)
        self.trust = MemberTrust(settings)
        self.verification = VerificationService(db, telegram, settings)
        self.cleanup_tasks = VerificationCleanup(db)
        self.progress = VerificationProgress(db, telegram, settings, self.trust.exempt_administrator)
        self.emoji_status = EmojiStatusGuard(db, telegram, settings, enqueue, self.trust.exempt_administrator)
        self.advertising = AdvertisingGuard(db, telegram, settings, self.trust)
        self.moderation = Moderation(db, telegram, settings, self.trust, self.emoji_status, self.advertising)
        self.members = MemberEvents(db, telegram, settings, self.trust, self.moderation, self.emoji_status)
        self.actions = ActionRunner(
            db,
            telegram,
            settings,
            self.verification,
            self.trust,
            self.progress,
            self.emoji_status,
            self.access,
            self.advertising,
        )
        self.action_claims = self.actions.action_claims

    cleanup = staticmethod(cleanup)
    restriction_identity = staticmethod(restriction_identity)
    owns_verification_ban = staticmethod(owns_verification_ban)

    async def member_event(self, event, bot_event=False, event_id=0):
        return await self.members.member_event(event, bot_event, event_id)

    async def message(self, message, edited=False, update_id=0, archived_at_receipt=False):
        return await self.moderation.message(message, edited, update_id, archived_at_receipt)

    async def join_request(self, event, event_id=0):
        return await self.verification.join_request(event, event_id)

    async def callback(self, query, *, acknowledge=True):
        return await self.verification.callback(query, acknowledge=acknowledge)

    async def acknowledge(self, identifier, answer):
        return await self.verification.acknowledge(identifier, answer)

    async def scan_timeouts(self):
        return await self.verification.scan_timeouts()

    async def check_permissions(self, group_id, join_requests=False):
        return await self.access.check_permissions(group_id, join_requests)

    async def run_one_action(self, *, include_progress=True, include_cleanup=True, lane=None):
        return await self.actions.run_one_action(
            include_progress=include_progress, include_cleanup=include_cleanup, lane=lane
        )

    async def run_one_cleanup(self):
        return await self.actions.run_one_cleanup()

    async def reconcile_verification_cleanup(self, limit=100):
        return await self.cleanup_tasks.reconcile_verification_cleanup(limit)

    async def action_loop(self, lane=None):
        return await self.actions.action_loop(lane)

    async def timeout_loop(self):
        return await self.verification.timeout_loop()

    async def cleanup_loop(self):
        return await Maintenance(
            self.db, self.tg, self.settings, self.cleanup_tasks, self.run_one_cleanup
        ).cleanup_loop()
