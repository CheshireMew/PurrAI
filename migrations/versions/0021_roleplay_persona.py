"""Replace the original default chat persona, preserving custom settings."""

import json

from alembic import op
import sqlalchemy as sa

revision = "0021"
down_revision = "0020"
branch_labels = depends_on = None


def upgrade():
    connection = op.get_bind()
    for identifier, saved in connection.execute(sa.text("SELECT id, settings FROM groups")).all():
        settings = json.loads(saved)
        if settings.get("chat_persona") != "随和、有一点幽默，关心群友，有自己的看法。":
            continue
        settings["chat_persona"] = (
            "随性、好奇，有一点幽默，跟群友相处熟络，用自然随意的群聊口吻说话。"
        )
        connection.execute(sa.text("UPDATE groups SET settings=:settings WHERE id=:id"),
                           {"id": identifier, "settings": json.dumps(settings, ensure_ascii=False)})


def downgrade():
    raise RuntimeError("Automatic destructive downgrade is not supported; restore a backup.")
