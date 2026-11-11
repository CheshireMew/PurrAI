"""Associate learned targets and rule matches with their observed source messages."""

import json

from alembic import op
import sqlalchemy as sa

revision = "0024"
down_revision = "0023"
branch_labels = depends_on = None


def upgrade():
    sources = op.create_table("promotion_sources",
        sa.Column("rule_id", sa.String(), primary_key=True),
        sa.Column("group_id", sa.BigInteger(), primary_key=True),
        sa.Column("message_id", sa.BigInteger(), primary_key=True),
        sa.Column("violation_id", sa.String(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("withdrawn_at", sa.Float(), nullable=False))
    op.create_index("ix_promotion_sources_violation_id", "promotion_sources", ["violation_id"])
    op.create_table("rule_hits",
        sa.Column("group_id", sa.BigInteger(), primary_key=True),
        sa.Column("rule_id", sa.String(), primary_key=True),
        sa.Column("message_id", sa.BigInteger(), primary_key=True),
        sa.Column("created_at", sa.Float(), nullable=False))
    connection = op.get_bind()
    raw = connection.execute(sa.text("SELECT rules FROM shared_policy WHERE id=1")).scalar()
    rules = json.loads(raw) if isinstance(raw, str) else raw or []
    targets = {item["value"]: item["id"] for item in rules if item["kind"] == "block_target"}
    seen = set()
    for group_id, payload, created in connection.execute(sa.text(
        "SELECT group_id,payload,created_at FROM actions WHERE dedupe_key LIKE 'learn-ad-targets:%'")):
        data = json.loads(payload) if isinstance(payload, str) else payload
        result = data.get("result", {})
        message_id = result.get("source_message_id")
        if not group_id or message_id is None:
            continue
        violation = connection.execute(sa.text(
            "SELECT id FROM violations WHERE group_id=:g AND message_id=:m"),
            {"g": group_id, "m": message_id}).scalar() or ""
        for target in result.get("targets", []):
            identifier = targets.get(target)
            key = identifier, group_id, message_id
            if identifier and key not in seen:
                connection.execute(sources.insert().values(rule_id=identifier, group_id=group_id,
                    message_id=message_id, violation_id=violation, created_at=created, withdrawn_at=0))
                seen.add(key)


def downgrade():
    # Keep the project's established non-destructive downgrade policy.
    raise RuntimeError("请恢复升级前的数据库备份与对应源码")
