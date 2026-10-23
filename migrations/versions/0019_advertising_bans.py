"""Enable explicit solicitation and external Telegram group policies."""

import json

from alembic import op
import sqlalchemy as sa

revision = "0019"
down_revision = "0018"
branch_labels = depends_on = None


def upgrade():
    connection = op.get_bind()
    for identifier, raw in connection.execute(sa.text("SELECT id,settings FROM groups")).all():
        settings = json.loads(raw)
        for key, default in {"advertising_ban": True, "external_group_ban": True,
                             "external_invite_ban": True, "telegram_allowed_links": []}.items():
            settings.setdefault(key, default)
        connection.execute(sa.text("UPDATE groups SET settings=:settings WHERE id=:id"),
                           {"settings": json.dumps(settings, ensure_ascii=False), "id": identifier})


def downgrade():
    raise RuntimeError("Automatic destructive downgrade is not supported; restore a backup.")
