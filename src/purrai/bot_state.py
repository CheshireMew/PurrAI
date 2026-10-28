"""Durable Telegram buttons, prompts and selected groups."""

import json
import secrets

from sqlalchemy import select

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

    async def buttons(self, actor, buttons, group_id, resume, *, panel_id=None, previous=None):
        keyboard = []
        async with self.db.transaction() as session:
            previous = previous or []
            keys = [f"bot_button:{item['callback_data'][2:]}" for item in previous]
            records = {
                row.key: json.loads(row.value)
                for row in await session.scalars(select(Meta).where(Meta.key.in_(keys)))
            } if keys else {}
            for index, (label, action, data) in enumerate(buttons):
                old = previous[index] if index < len(previous) else None
                record = records.get(f"bot_button:{old['callback_data'][2:]}") if old else None
                if record and record.get("panel") == panel_id and not record.get("processed") and (
                    record.get("expires") is None or record["expires"] > now()
                ):
                    keyboard.append(old)
                    continue
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
                                "panel": panel_id,
                            },
                            ensure_ascii=False,
                        ),
                    )
                )
                keyboard.append({"text": label[:60], "callback_data": f"a:{token}"})
        return keyboard

    async def publish_panel(self, actor, message_id, panel_id, group_id, resume, fingerprint, text_hash, keyboard):
        await self.remember(f"bot_panel:{actor}:{message_id}", {
            "panel": panel_id, "group_id": group_id, "resume": resume,
            "fingerprint": fingerprint, "text_hash": text_hash, "keyboard": keyboard,
        })

    async def complete_button(self, token):
        async with self.db.transaction() as session:
            row = await session.get(Meta, f"bot_button:{token}")
            if row:
                record = json.loads(row.value)
                record["processed"] = True
                row.value = json.dumps(record, ensure_ascii=False)
