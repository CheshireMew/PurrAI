"""Durable Telegram buttons, prompts and selected groups."""

import json
import secrets

from purrai.admin_labels import NAVIGATION_ACTIONS
from purrai.models import Meta, now


class BotState:
    def __init__(self, db):
        self.db = db

    async def remember(self, key, value):
        async with self.db.transaction() as session:
            row = await session.get(Meta, key)
            encoded = json.dumps(value, ensure_ascii=False)
            if row:
                row.value = encoded
            else:
                session.add(Meta(key=key, value=encoded))

    async def recall(self, key):
        async with self.db.sessions() as session:
            row = await session.get(Meta, key)
            return json.loads(row.value) if row else None

    async def buttons(self, actor, buttons, group_id, resume):
        keyboard = []
        async with self.db.transaction() as session:
            for label, action, data in buttons:
                token = secrets.token_hex(8)
                session.add(
                    Meta(
                        key=f"bot_button:{token}",
                        value=json.dumps(
                            {
                                "actor": actor,
                                "group_id": group_id,
                                "action": action,
                                "data": data,
                                "expires": None if action in NAVIGATION_ACTIONS else now() + 900,
                                "resume": resume,
                            },
                            ensure_ascii=False,
                        ),
                    )
                )
                keyboard.append({"text": label[:60], "callback_data": f"a:{token}"})
        return keyboard
