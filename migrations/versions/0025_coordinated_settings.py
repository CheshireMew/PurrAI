"""Explicit opt-in defaults for coordinated spam and temporary raid protection."""
import json

from alembic import op
import sqlalchemy as sa

revision = "0025"
down_revision = "0024"
branch_labels = depends_on = None

DEFAULTS = {"coordinated_enabled": False, "coordinated_window": 60, "coordinated_count": 4,
            "coordinated_min_length": 12, "raid_enabled": False, "raid_window": 60,
            "raid_count": 10, "emergency_seconds": 1800}


def upgrade():
    connection = op.get_bind()
    for table in ("groups", "shared_policy"):
        for identifier, raw in connection.execute(sa.text(f"SELECT id, settings FROM {table}")).all():
            settings = json.loads(raw) if isinstance(raw, str) else raw
            connection.execute(sa.text(f"UPDATE {table} SET settings=:settings WHERE id=:id"),
                               {"settings": json.dumps({**DEFAULTS, **settings}), "id": identifier})


def downgrade():
    pass  # Additive JSON fields can safely remain for a later upgrade.
