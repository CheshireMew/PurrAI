"""Extend the default newcomer observation to three days."""

import json

from alembic import op
import sqlalchemy as sa

revision = "0018"
down_revision = "0017"
branch_labels = depends_on = None


def upgrade():
    connection = op.get_bind()
    for identifier, saved in connection.execute(sa.text("SELECT id, settings FROM groups")).all():
        settings = json.loads(saved)
        # Preserve explicit custom periods, disabled checks and other settings.
        if settings.get("newcomer_seconds", 600) == 600:
            settings["newcomer_seconds"] = 259200
        settings.setdefault("newcomer_forwards", True)
        settings.setdefault("newcomer_wallets", True)
        connection.execute(sa.text("UPDATE groups SET settings=:settings WHERE id=:id"),
                           {"id": identifier, "settings": json.dumps(settings, ensure_ascii=False)})


def downgrade():
    raise RuntimeError("Automatic destructive downgrade is not supported; restore a backup.")
