"""Group message archive and observed revisions."""

import json
import uuid

from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "archived_messages",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("group_id", sa.BigInteger(), sa.ForeignKey("groups.id"), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("sender_id", sa.BigInteger(), nullable=True),
        sa.Column("sender_type", sa.String(), nullable=False),
        sa.Column("sender_name", sa.String(), nullable=False),
        sa.Column("username", sa.String(), nullable=False),
        sa.Column("message_type", sa.String(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("caption", sa.Text(), nullable=False),
        sa.Column("sent_at", sa.Float(), nullable=False),
        sa.Column("edited_at", sa.Float(), nullable=False),
        sa.Column("latest_update_id", sa.BigInteger(), nullable=False),
        sa.Column("latest_is_edit", sa.Boolean(), nullable=False),
        sa.Column("original_observed", sa.Boolean(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("revision_count", sa.Integer(), nullable=False),
        sa.Column("deletion_state", sa.String(), nullable=False),
        sa.Column("deletion_error", sa.Text(), nullable=False),
        sa.Column("deletion_action_id", sa.String(), nullable=False),
        sa.Column("deleted_at", sa.Float(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.UniqueConstraint("group_id", "message_id"),
    )
    op.create_index("ix_archive_group_date", "archived_messages", ["group_id", "sent_at", "message_id"])
    op.create_index("ix_archive_sender_date", "archived_messages", ["group_id", "sender_id", "sent_at"])
    op.create_index("ix_archived_messages_deletion_state", "archived_messages", ["deletion_state"])
    op.create_table(
        "message_revisions",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("archive_id", sa.String(), sa.ForeignKey("archived_messages.id"), nullable=False),
        sa.Column("version_key", sa.String(), nullable=False),
        sa.Column("update_id", sa.BigInteger(), nullable=False),
        sa.Column("is_edit", sa.Boolean(), nullable=False),
        sa.Column("event_at", sa.Float(), nullable=False),
        sa.Column("received_at", sa.Float(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("reasons", sa.JSON(), nullable=False),
        sa.UniqueConstraint("archive_id", "version_key"),
    )
    op.create_index("ix_message_revisions_archive_id", "message_revisions", ["archive_id"])
    connection = op.get_bind()
    for row in connection.execute(sa.text("SELECT id,settings FROM groups")).mappings().all():
        settings = json.loads(row["settings"])
        settings.setdefault("archive_enabled", True)
        connection.execute(
            sa.text("UPDATE groups SET settings=:settings WHERE id=:id"),
            {"id": row["id"], "settings": json.dumps(settings)},
        )
    # Old data contains excerpts only. Preserve them without pretending to recover full history.
    for row in connection.execute(sa.text("SELECT * FROM violations WHERE message_id>0")).mappings().all():
        identifier = uuid.uuid4().hex
        connection.execute(
            sa.text("""INSERT INTO archived_messages
            (id,group_id,message_id,sender_id,sender_type,sender_name,username,message_type,text,caption,
             sent_at,edited_at,latest_update_id,latest_is_edit,original_observed,source,revision_count,
             deletion_state,deletion_error,deletion_action_id,deleted_at,created_at,updated_at)
            VALUES (:id,:group_id,:message_id,:user_id,:sender_type,:sender_name,'','unknown',:excerpt,'',
             :created_at,0,0,0,0,'violation_excerpt',1,'not_requested','','',0,:created_at,:created_at)"""),
            {**dict(row), "id": identifier, "sender_type": "chat" if row["sender_chat"] else "user"},
        )
        connection.execute(
            sa.text("""INSERT INTO message_revisions
            (archive_id,version_key,update_id,is_edit,event_at,received_at,payload,reasons)
            VALUES (:archive_id,'legacy',0,0,:created_at,:created_at,:payload,:reasons)"""),
            {
                "archive_id": identifier,
                "created_at": row["created_at"],
                "reasons": row["reasons"],
                "payload": json.dumps({"text": row["excerpt"], "partial": True, "sent_at_unknown": True}),
            },
        )
    for row in (
        connection.execute(sa.text("SELECT * FROM actions WHERE kind='delete' ORDER BY created_at"))
        .mappings()
        .all()
    ):
        payload = json.loads(row["payload"])
        if payload.get("chat_id", row["group_id"]) != row["group_id"]:
            continue
        state = {"succeeded": "deleted", "pending": "pending", "failed": "failed"}.get(
            row["state"], "not_requested"
        )
        connection.execute(
            sa.text("""UPDATE archived_messages SET deletion_state=:state,
            deletion_error=:error,deletion_action_id=:id,deleted_at=:deleted_at
            WHERE group_id=:group_id AND message_id=:message_id"""),
            {
                **dict(row),
                "message_id": payload.get("message_id", 0),
                "state": state,
                "deleted_at": row["updated_at"] if state == "deleted" else 0,
            },
        )


def downgrade():
    raise RuntimeError("Automatic destructive downgrade is not supported; restore a backup.")
