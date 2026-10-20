"""Persist probation counts, content summaries, profile rules and join requests."""

import json

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    additions = {
        "members": [("observed_messages", sa.Integer(), "0"), ("request_until", sa.Float(), "0")],
        "verifications": [("flow", sa.String(), "joined"), ("request_date", sa.Float(), "0")],
        "rules": [
            ("scope", sa.String(), "message"),
            ("context_value", sa.String(), ""),
        ],
        "violations": [
            ("sender_name", sa.String(), ""),
            ("profile_violation", sa.Boolean(), "0"),
            ("message_violation", sa.Boolean(), "1"),
        ],
        "fingerprints": [
            ("generation", sa.String(), ""),
            ("sequence", sa.Integer(), "0"),
            ("batch_key", sa.String(), ""),
            ("similarity_hash", sa.String(), ""),
            ("similarity_length", sa.Integer(), "0"),
        ],
    }
    for table, fields in additions.items():
        for name, kind, default in fields:
            op.add_column(table, sa.Column(name, kind, nullable=False, server_default=default))
    op.add_column("verifications", sa.Column("prompt_chat_id", sa.BigInteger(), nullable=True))
    # Existing members have no reliable historical message count; never reapply a first-message rule.
    op.execute("UPDATE members SET observed_messages=1000000")
    op.execute("UPDATE fingerprints SET batch_key='m:' || CAST(message_id AS TEXT)")
    op.execute(
        "UPDATE actions SET state='cancelled', error='正文命中改为只删消息，旧自动处罚已取消' WHERE state='pending' AND kind IN ('moderation_mute', 'warn')"
    )
    defaults = {
        "join_requests": False,
        "newcomer_messages": 3,
        "newcomer_mentions": True,
        "similar_enabled": True,
        "similar_window": 60,
        "similar_count": 4,
        "similar_threshold": 85,
        "similar_min_length": 12,
        "burst_enabled": True,
        "burst_window": 10,
        "burst_count": 8,
    }
    connection = op.get_bind()
    for identifier, raw in connection.execute(sa.text("SELECT id, settings FROM groups")).all():
        settings = json.loads(raw)
        if settings.get("mute_seconds", 1800) == 1800:
            settings["mute_seconds"] = 259200
        connection.execute(
            sa.text("UPDATE groups SET settings=:value WHERE id=:id"),
            {"id": identifier, "value": json.dumps({**defaults, **settings})},
        )


def downgrade():
    raise RuntimeError("Automatic destructive downgrade is not supported; restore a backup.")
