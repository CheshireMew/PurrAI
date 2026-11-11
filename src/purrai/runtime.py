"""Compose event receipt, ordered processing and worker supervision."""

from purrai.bot_admin import BotAdmin
from purrai.chat import GroupChat
from purrai.inbox import InboxProcessor
from purrai.management import Management
from purrai.telegram_events import TelegramEvents
from purrai.update_receiver import UpdateReceiver
from purrai.worker_supervisor import WorkerSupervisor
from purrai.runtime_alerts import RuntimeAlerts


class Runtime:
    def __init__(self, db, telegram, service):
        self.db, self.tg, self.service = db, telegram, service
        self.chat = GroupChat(db, telegram, service.settings)
        self.management = Management(db, telegram, settings=service.settings, chat=self.chat, access=service.access)
        self.admin = BotAdmin(self.management)
        self.events = TelegramEvents(service, self.admin, self.chat)
        self.inbox = InboxProcessor(db, telegram, service.settings, self.events.dispatch, self.events.current_update_id)
        self.receiver = UpdateReceiver(db, telegram, service.settings, self.inbox.ready_events)
        self.workers = WorkerSupervisor(service.settings)
        self.alerts = RuntimeAlerts(db, service.settings, self.status)

    async def persist(self, updates):
        return await self.receiver.persist(updates)

    async def process_one(self, lane=None):
        return await self.inbox.process_one(lane)

    def start(self):
        if self.tg.bot:
            self.workers.start([
                ("telegram-poll", self.receiver.poll_loop),
                *[(f"inbox-messages-{i}", self.inbox.inbox_loop) for i in range(4)],
                *[(f"inbox-control-{i}", lambda: self.inbox.inbox_loop("control")) for i in range(2)],
                ("inbox-verification", lambda: self.inbox.inbox_loop("verification")),
                *[(f"actions-members-{i}", lambda: self.service.action_loop("member")) for i in range(4)],
                *[(f"actions-notifications-{i}", lambda: self.service.action_loop("notification")) for i in range(2)],
                ("verification-timeouts", self.service.timeout_loop),
                ("verification-cleanup", self.service.cleanup_loop),
                ("verification-progress", self.service.progress.loop),
                ("emoji-status", self.service.emoji_status.loop),
                ("advertising-links", self.service.advertising.loop),
                ("chat", self.chat.run),
                ("operations-maintenance", self.maintenance_loop),
            ])

    def ready(self):
        return self.workers.ready()

    async def maintenance_loop(self):
        import asyncio
        while True:
            await self.alerts.cycle()
            await self.management.backups.cycle()
            await self.management.protection.cycle()
            await asyncio.sleep(30)

    def status(self):
        return {**self.workers.status(self.receiver.last_error), "poll_error": self.receiver.last_error}

    async def stop(self):
        await self.workers.stop()
        await self.events.close()
        await self.admin.ui.close()
        await self.chat.close()
        await self.tg.close()
