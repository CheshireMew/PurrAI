"""Default requested status-emoji pack blacklist for managed groups."""

import json
from alembic import op
import sqlalchemy as sa

revision = "0008"
down_revision = "0007"
branch_labels = depends_on = None


def upgrade():
    connection = op.get_bind()
    for row in connection.execute(sa.text("SELECT id,settings FROM groups")).mappings().all():
        settings = json.loads(row["settings"])
        settings.setdefault("emoji_status_blocked_packs", ["bygbb666"])
        connection.execute(
            sa.text("UPDATE groups SET settings=:settings WHERE id=:id"),
            {"id": row["id"], "settings": json.dumps(settings, ensure_ascii=False)},
        )


def downgrade():
    raise RuntimeError("Automatic destructive downgrade is not supported; restore a backup.")
