"""Rename the default chat persona without replacing custom group names."""

import json

from alembic import op
import sqlalchemy as sa

revision = "0009"
down_revision = "0008"
branch_labels = depends_on = None


def upgrade():
    connection = op.get_bind()
    for row in connection.execute(sa.text("SELECT id,settings FROM groups")).mappings().all():
        settings = json.loads(row["settings"])
        if settings.get("chat_name", "小喵") != "小喵":
            continue
        settings["chat_name"] = "柴郡喵"
        connection.execute(
            sa.text("UPDATE groups SET settings=:settings WHERE id=:id"),
            {"id": row["id"], "settings": json.dumps(settings, ensure_ascii=False)},
        )


def downgrade():
    raise RuntimeError("Automatic destructive downgrade is not supported; restore a backup.")
