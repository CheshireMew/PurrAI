"""Bounded group chat context and independent durable reply tasks."""

import json

from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "chat_messages",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("group_id", sa.BigInteger(), sa.ForeignKey("groups.id"), nullable=False),
        sa.Column("topic_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("sender_id", sa.BigInteger(), nullable=False),
        sa.Column("sender_name", sa.String(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("reply_to_id", sa.BigInteger(), nullable=False),
        sa.Column("reply_text", sa.Text(), nullable=False),
        sa.Column("edited_at", sa.Float(), nullable=False),
        sa.Column("update_id", sa.BigInteger(), nullable=False),
        sa.Column("is_bot", sa.Boolean(), nullable=False),
        sa.Column("eligible", sa.Boolean(), nullable=False),
        sa.Column("sent_at", sa.Float(), nullable=False),
        sa.Column("received_at", sa.Float(), nullable=False),
        sa.UniqueConstraint("group_id", "message_id"),
    )
    op.create_index("ix_chat_context", "chat_messages", ["group_id", "topic_id", "sent_at", "message_id"])
    op.create_table(
        "chat_turns",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("group_id", sa.BigInteger(), sa.ForeignKey("groups.id"), nullable=False),
        sa.Column("topic_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("direct", sa.Boolean(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.Float(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("response", sa.Text(), nullable=False),
        sa.Column("sent_message_id", sa.BigInteger(), nullable=True),
        sa.Column("error", sa.String(), nullable=False),
        sa.UniqueConstraint("group_id", "message_id"),
    )
    op.create_index("ix_chat_pending", "chat_turns", ["state", "available_at"])
    defaults = {
        "chat_enabled": False, "chat_proactive": True, "chat_name": "小喵",
        "chat_persona": "随和、有一点幽默，关心群友，有自己的看法。",
        "chat_history": 40, "chat_interval": 90,
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
