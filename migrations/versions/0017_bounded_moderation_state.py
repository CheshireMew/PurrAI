"""Bound auxiliary message state and index moderation lookups."""

import json
import time
import uuid

from alembic import op
import sqlalchemy as sa

revision = "0017"
down_revision = "0016"
branch_labels = depends_on = None


def upgrade():
    op.create_index("ix_fingerprints_member_window", "fingerprints",
                    ["group_id", "user_id", "generation", "created_at", "message_id"])
    op.create_index("ix_fingerprints_batch", "fingerprints", ["group_id", "user_id", "generation", "batch_key"])
    connection = op.get_bind()
    # Retain the first 100 distinct contributions, including their message IDs:
    # counts at every supported threshold and earlier-media checks remain valid.
    connection.execute(sa.text("""
        DELETE FROM member_texts WHERE rowid IN (
            SELECT item FROM (
                SELECT rowid AS item, ROW_NUMBER() OVER (
                    PARTITION BY group_id,user_id ORDER BY message_id
                ) AS position FROM member_texts
            ) WHERE position > 100
        )
    """))
    connection.execute(sa.text("""
        DELETE FROM fingerprints WHERE NOT (
            sequence BETWEEN 1 AND 100 AND EXISTS (
                SELECT 1 FROM members WHERE members.group_id=fingerprints.group_id
                AND members.user_id=fingerprints.user_id AND members.generation=fingerprints.generation
            )
        ) AND (created_at < :cutoff OR rowid IN (
            SELECT item FROM (
                SELECT rowid AS item, ROW_NUMBER() OVER (
                    PARTITION BY group_id,user_id,generation ORDER BY created_at DESC,message_id DESC
                ) AS position FROM fingerprints
            ) WHERE position > 500
        ))
    """), {"cutoff": time.time() - 3600})
    # Older versions reused one release/failure key across reissues. Recover
    # only the stranded transitions with an obsolete legacy task; retain any
    # current pending/failed task so migration never silently retries a failure.
    stranded = connection.execute(sa.text("""
        SELECT v.id,v.group_id,v.user_id,v.nonce,v.state,v.error FROM verifications v
        JOIN actions a ON a.dedupe_key = CASE v.state
            WHEN 'releasing' THEN 'release:' ELSE 'verification-failure:' END || v.id
        WHERE (v.state='releasing' OR (v.state='failing' AND v.error IN ('验证超时','错误次数已用尽')))
        AND json_extract(a.payload,'$.expected_nonce') IS NOT NULL
        AND json_extract(a.payload,'$.expected_nonce') != v.nonce
        AND NOT EXISTS (
            SELECT 1 FROM actions current WHERE json_extract(current.payload,'$.verification_id')=v.id
            AND json_extract(current.payload,'$.expected_nonce')=v.nonce
        )
    """)).all()
    for identifier, group_id, user_id, nonce, state, reason in stranded:
        kind = "release" if state == "releasing" else "verification_failure"
        prefix = "release" if state == "releasing" else "verification-failure"
        connection.execute(sa.text("""
            INSERT INTO actions (id,dedupe_key,group_id,user_id,kind,payload,state,priority,attempts,
                                 available_at,error,actor_id,reason,created_at,updated_at,batch_id)
            VALUES (:id,:key,:group,:user,:kind,:payload,'pending',0,0,:at,'',NULL,:reason,:at,:at,NULL)
        """), {"id": uuid.uuid4().hex, "key": f"{prefix}:{identifier}:{nonce}", "group": group_id,
               "user": user_id, "kind": kind, "at": time.time(), "reason": reason,
               "payload": json.dumps({"verification_id": identifier, "expected_nonce": nonce})})


def downgrade():
    raise RuntimeError("Automatic destructive downgrade is not supported; restore a backup.")
