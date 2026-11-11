"""Bound disposable interaction records without deleting business history."""

from alembic import op
import sqlalchemy as sa

revision = "0023"
down_revision = "0022"
branch_labels = depends_on = None


def upgrade():
    op.add_column("meta", sa.Column("expires_at", sa.Float(), nullable=False, server_default="0"))
    op.create_index("ix_meta_expires_at", "meta", ["expires_at"])
    op.add_column("inbox", sa.Column("completed_at", sa.Float(), nullable=False, server_default="0"))
    op.create_index("ix_inbox_completed_at", "inbox", ["completed_at"])
    connection = op.get_bind()
    connection.execute(sa.text("""
        INSERT INTO meta (key,value,expires_at)
        SELECT 'archive:stored-bytes', cast(
            (SELECT coalesce(sum(length(cast(payload AS BLOB))+length(cast(reasons AS BLOB))+512),0) FROM message_revisions)
            + (SELECT coalesce(sum(length(cast(text AS BLOB))+length(cast(caption AS BLOB))+
                length(cast(sender_name AS BLOB))+length(cast(username AS BLOB))+
                length(cast(deletion_error AS BLOB))+512),0) FROM archived_messages)
            AS TEXT), 0
        WHERE NOT EXISTS (SELECT 1 FROM meta WHERE key='archive:stored-bytes')
    """))
    # Allow seven days from upgrade for old callbacks/redeliveries. Old panel
    # buttons remain recoverable for thirty days; no immediate purge occurs.
    import time
    stamp = time.time()
    connection.execute(sa.text("UPDATE inbox SET completed_at=:stamp WHERE state='done'"), {"stamp": stamp})
    for prefix, seconds in (("callback:", 7 * 86400), ("join-request:", 7 * 86400),
                            ("bot_button:", 30 * 86400), ("bot_panel:", 30 * 86400),
                            ("bot_prompt:", 7 * 86400)):
        connection.execute(sa.text("UPDATE meta SET expires_at=:expires WHERE key LIKE :prefix"),
                           {"expires": stamp + seconds, "prefix": prefix + "%"})


def downgrade():
    op.drop_index("ix_inbox_completed_at", "inbox")
    op.drop_column("inbox", "completed_at")
    op.drop_index("ix_meta_expires_at", "meta")
    op.drop_column("meta", "expires_at")
