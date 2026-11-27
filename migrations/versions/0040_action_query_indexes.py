"""Indexed task ordering and violation associations without changing task intent."""

from alembic import op
import sqlalchemy as sa

revision = "0040"
down_revision = "0039"
branch_labels = depends_on = None

# Frozen queue identity for this schema revision. Runtime classification changes
# must migrate the expression so saved and newly received tasks agree.
SCOPE = """CASE
    WHEN kind = 'group_menu' THEN json_array('group_menu', group_id)
    WHEN kind = 'points_message' AND json_extract(payload, '$.operation') = 'chat'
        THEN json_array('points_reward', group_id, user_id)
    WHEN kind IN ('linked_channel_unpin','warn','admin_notify','archive_export','join_link',
        'owner_alert','auto_reply','points_message','lottery_notice','group_menu')
        THEN json_array('notification', group_id,
            coalesce(nullif(actor_id, 0), nullif(json_extract(payload, '$.chat_id'), 0), user_id))
    WHEN user_id IS NOT NULL AND user_id != 0 THEN json_array('member', group_id, user_id)
    ELSE json_array('action', id) END"""


def upgrade():
    connection = op.get_bind()
    if "queue_scope" not in {column["name"] for column in sa.inspect(connection).get_columns("actions")}:
        op.add_column("actions", sa.Column("queue_scope", sa.String(), sa.Computed(SCOPE, persisted=False)))
    op.execute("CREATE INDEX IF NOT EXISTS ix_actions_due ON actions(state, available_at)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_actions_scope_order ON actions(queue_scope, state, priority, created_at)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_actions_violation ON actions(group_id, json_extract(payload, '$.violation_id'))")
    op.execute("CREATE INDEX IF NOT EXISTS ix_points_pending ON actions(group_id, created_at) WHERE state = 'pending' AND kind = 'points_message' AND json_extract(payload, '$.operation') = 'chat'")


def downgrade():
    pass
