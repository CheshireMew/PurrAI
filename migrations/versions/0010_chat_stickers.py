"""Default chat sticker packs and occasional anti-fraud reminders."""

import json

from alembic import op
import sqlalchemy as sa

revision = "0010"
down_revision = "0009"
branch_labels = depends_on = None


def upgrade():
    defaults = {
        "chat_stickers": True, "chat_sticker_interval": 60,
        "chat_sticker_packs": [
            "myadestes", "Beach_Bat_Responses_by_fStikBot", "poi466", "LLMG_GIF", "cookprivet",
            "tofupack_by_fStikBot", "catvideopack_by_fStikBot", "superCheshire", "sumyy3", "HMS_Cheshire",
            "p1_wmtz1349120938_by_WuMingv2Bot", "Cheshire_the_kitten", "sticker_ab2f7c92_by_moe_sticker_bot",
            "sese11_by_WuMingv2Bot", "p0_wmtz1678979585_by_WuMingv2Bot", "p0_wmtz1296407908_by_WuMingv2Bot",
            "GreatCheshire", "BigCheshire", "goodCheshire", "AwesomeCheshireCat",
        ],
        "chat_antifraud": True, "chat_antifraud_packs": ["FTX888"], "chat_antifraud_interval": 21600,
    }
    connection = op.get_bind()
    for row in connection.execute(sa.text("SELECT id,settings FROM groups")).mappings().all():
        settings = {**defaults, **json.loads(row["settings"])}
        connection.execute(
            sa.text("UPDATE groups SET settings=:settings WHERE id=:id"),
            {"id": row["id"], "settings": json.dumps(settings, ensure_ascii=False)},
        )


def downgrade():
    raise RuntimeError("Automatic destructive downgrade is not supported; restore a backup.")
