"""Persistent independent text contributions before visual media is allowed."""

import json

from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "member_texts",
        sa.Column("group_id", sa.BigInteger(), sa.ForeignKey("groups.id"), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), primary_key=True),
        sa.Column("message_id", sa.BigInteger(), primary_key=True),
        sa.Column("digest", sa.String(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.UniqueConstraint("group_id", "user_id", "digest"),
    )
    connection = op.get_bind()
    for row in connection.execute(sa.text("SELECT id,settings FROM groups")).mappings().all():
        settings = json.loads(row["settings"])
        settings.setdefault("media_min_text_messages", 3)
        connection.execute(
            sa.text("UPDATE groups SET settings=:settings WHERE id=:id"),
            {"id": row["id"], "settings": json.dumps(settings, ensure_ascii=False)},
        )


def downgrade():
    raise RuntimeError("Automatic destructive downgrade is not supported; restore a backup.")
