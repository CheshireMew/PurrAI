"""Selectable verification failures; no automatic temporary bans."""

import json

from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = depends_on = None


def upgrade():
    op.add_column(
        "verifications", sa.Column("failure_action", sa.String(), nullable=False, server_default="")
    )
    connection = op.get_bind()
    for row in connection.execute(sa.text("SELECT id,settings FROM groups")).mappings().all():
        settings = json.loads(row["settings"])
        settings.setdefault("verification_failure_action", "permanent_mute")
        connection.execute(
            sa.text("UPDATE groups SET settings=:settings WHERE id=:id"),
            {"id": row["id"], "settings": json.dumps(settings)},
        )
    connection.execute(
        sa.text(
            "UPDATE verifications SET failure_action=CASE WHEN flow='join_request' THEN 'decline' ELSE 'legacy_ban' END WHERE state='failed'"
        )
    )
    for row in (
        connection.execute(
            sa.text(
                "SELECT id,payload FROM actions WHERE kind='ban' AND actor_id IS NULL AND state IN ('pending','failed')"
            )
        )
        .mappings()
        .all()
    ):
        payload = json.loads(row["payload"])
        identifier = payload.get("verification_id")
        if not identifier:
            continue
        connection.execute(
            sa.text("UPDATE actions SET kind='verification_failure' WHERE id=:id"), {"id": row["id"]}
        )
        connection.execute(
            sa.text("UPDATE verifications SET state='failing' WHERE id=:id AND state='removing'"),
            {"id": identifier},
        )


def downgrade():
    raise RuntimeError("Automatic destructive downgrade is not supported; restore a backup.")
