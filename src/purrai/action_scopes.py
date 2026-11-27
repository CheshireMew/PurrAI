"""SQLite's computed ordering key uses the same notification classification as policy."""

from purrai.action_specs import NOTIFICATION_ACTIONS


def scope_sql():
    kinds = ",".join("'" + kind.replace("'", "''") + "'" for kind in NOTIFICATION_ACTIONS)
    return f"""CASE
        WHEN kind = 'group_menu' THEN json_array('group_menu', group_id)
        WHEN kind = 'points_message' AND json_extract(payload, '$.operation') = 'chat'
            THEN json_array('points_reward', group_id, user_id)
        WHEN kind IN ({kinds}) THEN json_array('notification', group_id,
            coalesce(nullif(actor_id, 0), nullif(json_extract(payload, '$.chat_id'), 0), user_id))
        WHEN user_id IS NOT NULL AND user_id != 0 THEN json_array('member', group_id, user_id)
        ELSE json_array('action', id) END"""
