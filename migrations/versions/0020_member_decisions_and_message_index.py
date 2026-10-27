"""Fence old member decisions and index exact-message moderation lookups."""

import time

from alembic import op
import sqlalchemy as sa

revision = "0020"
down_revision = "0019"
branch_labels = depends_on = None


def upgrade():
    op.create_index("ix_actions_message", "actions",
                    ["group_id", "kind", sa.text("json_extract(payload, '$.message_id')")])
    op.create_index("ix_actions_member_pending", "actions", ["group_id", "user_id", "state"])
    connection = op.get_bind()
    # Preserve every receipt. Only the latest explicit decision may still run
    # for a member; this also fences failed historical jobs against manual retry.
    connection.execute(sa.text("""
        INSERT OR IGNORE INTO meta (key,value)
        SELECT 'member-decision:' || group_id || ':' || user_id, id FROM (
            SELECT id,group_id,user_id,ROW_NUMBER() OVER (
                PARTITION BY group_id,user_id ORDER BY created_at DESC,rowid DESC
            ) AS position FROM actions
            WHERE actor_id IS NOT NULL AND group_id IS NOT NULL AND user_id IS NOT NULL
            AND kind IN ('verify_mute','challenge','release','ban','manual_ban','manual_mute','unmute','unban')
        ) WHERE position=1
    """))
    connection.execute(sa.text("""
        UPDATE actions SET state='cancelled',error='管理员已作出新的成员处理决定',updated_at=:at
        WHERE state='pending' AND actor_id IS NOT NULL
        AND kind IN ('verify_mute','challenge','release','ban','manual_ban','manual_mute','unmute','unban')
        AND EXISTS (SELECT 1 FROM meta
            WHERE key='member-decision:' || actions.group_id || ':' || actions.user_id
            AND value != actions.id)
    """), {"at": time.time()})
    # An already-owned restriction provides reliable provenance for completing
    # a lost receipt. Unsent unbound profile jobs cannot be attributed to a join.
    connection.execute(sa.text("""
        UPDATE actions SET payload=json_set(payload,'$.generation',(
            SELECT generation FROM members m
            WHERE m.group_id=actions.group_id AND m.user_id=actions.user_id
        )) WHERE kind='profile_mute' AND state='pending'
        AND coalesce(json_extract(payload,'$.generation'),'')=''
        AND EXISTS (SELECT 1 FROM members m
            WHERE m.group_id=actions.group_id AND m.user_id=actions.user_id
            AND json_extract(m.owned_restriction,'$.source_action')=actions.id)
    """))
    connection.execute(sa.text("""
        UPDATE actions SET state='cancelled',
            error='旧昵称处罚缺少入群批次，已停止执行；请重新核实成员',updated_at=:at
        WHERE kind='profile_mute' AND state='pending'
        AND coalesce(json_extract(payload,'$.generation'),'')=''
    """), {"at": time.time()})


def downgrade():
    raise RuntimeError("Automatic destructive downgrade is not supported; restore a backup.")
