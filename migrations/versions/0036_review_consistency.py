"""Correct historical AI counts and preserve Telegram message namespaces in points entries."""
import json

from alembic import op
import sqlalchemy as sa

revision = "0036"
down_revision = "0035"
branch_labels = depends_on = None


def upgrade():
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if "local_after" not in {c["name"] for c in inspector.get_columns("advertisement_reviews")}:
        op.add_column("advertisement_reviews", sa.Column("local_after", sa.Float(), nullable=False, server_default="0"))
    if "ix_ad_review_local" not in {i["name"] for i in inspector.get_indexes("advertisement_reviews")}:
        op.create_index("ix_ad_review_local", "advertisement_reviews", ["state", "local_after"])

    meta = sa.table("meta", sa.column("key"), sa.column("value"))
    violations = sa.table("violations", sa.column("id"), sa.column("group_id"), sa.column("user_id"), sa.column("message_id"))
    samples = sa.table("advertisement_samples", sa.column("violation_id"), sa.column("version"),
                       sa.column("enabled"), sa.column("review_state"))
    corrected = sa.select(violations).join(meta, meta.c.key == sa.literal("violation-correction:") + violations.c.id)
    by_member = {}
    for _, group, user, message in conn.execute(corrected):
        by_member.setdefault((group, user), set()).add(message)
    for (group, user), messages in by_member.items():
        key = f"ai_advertising:{group}:{user}"
        value = conn.scalar(sa.select(meta.c.value).where(meta.c.key == key))
        if value:
            data = json.loads(value)
            data["messages"] = [item for item in data.get("messages", []) if item["id"] not in messages]
            conn.execute(meta.update().where(meta.c.key == key).values(value=json.dumps(data, ensure_ascii=False)))
    conn.execute(samples.update().where(samples.c.enabled == 1, samples.c.violation_id.in_(
        corrected.with_only_columns(violations.c.id))).values(enabled=False, review_state="done", version=samples.c.version + 1))

    # The immutable balance receipt retains the original group even after a supergroup upgrade.
    entries = sa.table("points_entries", sa.column("id"), sa.column("group_id"), sa.column("request_id"),
                       sa.column("kind"), sa.column("result", sa.JSON()), sa.column("intent", sa.JSON()))
    for identifier, group, key, kind, result, intent in conn.execute(sa.select(entries).where(
            entries.c.kind.in_(("chat", "adjustment")))):
        prefix, _, number = key.partition(":")
        if (prefix == ("chat" if kind == "chat" else "admin-adjust") and number.isdecimal()
                and "kind" not in intent):  # API request IDs are supplied by the caller.
            origin = result.get("group_id", group)
            conn.execute(entries.update().where(entries.c.id == identifier)
                         .values(request_id=f"{prefix}:{origin}:{number}"))


def downgrade():
    # Keep accounting provenance and corrected evidence across a rollback.
    pass
