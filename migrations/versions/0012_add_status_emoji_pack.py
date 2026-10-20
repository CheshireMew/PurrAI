"""Append the additional status emoji pack requested by the owner."""

import json

from alembic import op
import sqlalchemy as sa

revision = "0012"
down_revision = "0011"
branch_labels = depends_on = None


def upgrade():
    connection = op.get_bind()
    for row in connection.execute(sa.text("SELECT id,settings FROM groups")).mappings().all():
        settings = json.loads(row["settings"])
        packs = settings.get("emoji_status_blocked_packs", ["bygbb666"])
        if any(name.lower() == "gshechaba" for name in packs):
            continue
        settings["emoji_status_blocked_packs"] = [*packs, "gshechaba"]
        connection.execute(
            sa.text("UPDATE groups SET settings=:settings WHERE id=:id"),
            {"id": row["id"], "settings": json.dumps(settings, ensure_ascii=False)},
        )


def downgrade():
    raise RuntimeError("Automatic destructive downgrade is not supported; restore a backup.")
