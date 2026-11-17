"""Preserve existing policy while exposing decisions, timing and review criteria."""
import copy
import json
from importlib.resources import files

from alembic import op
import sqlalchemy as sa

revision = "0034"
down_revision = "0033"
branch_labels = depends_on = None

SETTINGS = {
    "notice_seconds": 30, "ai_ad_window_hours": 72, "ai_ad_limit": 3,
    "ai_ad_penalty": "permanent_mute", "spam_mute_seconds": 1800, "media_newcomer_days": 15,
    "learned_target_penalty": "ban", "chat_advertiser_cooldown_seconds": 1800,
    "chat_evidence_hours": 168, "chat_command_min_users": 3, "chat_min_opening_chars": 4,
    "chat_ignore_numeric_openings": True, "chat_repeat_hours": 24,
}


def upgrade():
    conn = op.get_bind()
    if "name_penalty" not in {c["name"] for c in sa.inspect(conn).get_columns("rules")}:
        op.add_column("rules", sa.Column("name_penalty", sa.String(), nullable=False, server_default="permanent_mute"))
    meta = sa.table("meta", sa.column("key", sa.String()), sa.column("value", sa.Text()), sa.column("expires_at", sa.Float()))
    marker = "policy-migration:0034"
    if conn.execute(sa.select(meta.c.key).where(meta.c.key == marker)).first():
        return
    groups = sa.table("groups", sa.column("id", sa.BigInteger()), sa.column("settings", sa.JSON()))
    group_settings = {}
    for identifier, settings in conn.execute(sa.select(groups.c.id, groups.c.settings)):
        updated = {**SETTINGS, **settings}
        group_settings[identifier] = updated
        conn.execute(groups.update().where(groups.c.id == identifier).values(settings=updated))
    seed = json.loads(files("purrai").joinpath("assets/business_rules.v2.json").read_text(encoding="utf-8"))
    policies = sa.table("shared_policy", sa.column("id", sa.Integer()), sa.column("settings", sa.JSON()),
                        sa.column("rules", sa.JSON()), sa.column("business_rules", sa.JSON()))
    for identifier, settings, rules, document in conn.execute(sa.select(policies)):
        rules, document = copy.deepcopy(rules), copy.deepcopy(document)
        for rule in rules:
            rule.setdefault("name_penalty", "permanent_mute")
            if rule["kind"] == "block_target":
                rule["penalty"] = "ban"  # This type previously always banned, ignoring its stored field.
        document.setdefault("ai_review_instructions", seed["ai_review_instructions"])
        for stage in document["stages"]:
            for rule in stage["rules"]:
                rule.setdefault("advertiser_cooldown", rule["action"] == "ban" or rule["reason"].startswith(
                    ("广告招揽：", "明确广告引流：", "聊天广告历史命中")))
        conn.execute(policies.update().where(policies.c.id == identifier).values(
            settings={**SETTINGS, **settings}, rules=rules, business_rules=document))
    rules = sa.table("rules", sa.column("kind", sa.String()), sa.column("penalty", sa.String()))
    conn.execute(rules.update().where(rules.c.kind == "block_target").values(penalty="ban"))
    for key, value in conn.execute(sa.select(meta.c.key, meta.c.value).where(meta.c.key.startswith("chat_input:"))):
        data = json.loads(value)
        data["advertisers"] = {key: {"at": value - 1800} if isinstance(value, (int, float)) else value
                               for key, value in data.get("advertisers", {}).items()}
        conn.execute(meta.update().where(meta.c.key == key).values(value=json.dumps(data)))
    actions = sa.table("actions", sa.column("id", sa.String()), sa.column("dedupe_key", sa.String()),
                       sa.column("kind", sa.String()), sa.column("group_id", sa.BigInteger()),
                       sa.column("state", sa.String()), sa.column("updated_at", sa.Float()),
                       sa.column("available_at", sa.Float()), sa.column("payload", sa.JSON()))
    for identifier, key, group_id in conn.execute(sa.select(actions.c.id, actions.c.dedupe_key, actions.c.group_id).where(
            actions.c.kind == "delete", actions.c.state == "pending", actions.c.dedupe_key.startswith("cleanup:notice:"))):
        notice_id = key.removeprefix("cleanup:notice:")
        notice = conn.execute(sa.select(actions.c.updated_at, actions.c.payload).where(actions.c.id == notice_id)).first()
        if notice and group_id in group_settings:
            deadline = notice.updated_at + group_settings[group_id]["notice_seconds"]
            conn.execute(actions.update().where(actions.c.id == identifier).values(available_at=deadline))
            conn.execute(actions.update().where(actions.c.id == notice_id).values(
                payload={**notice.payload, "notice_expires_at": deadline}))
    conn.execute(meta.insert().values(key=marker, value="complete", expires_at=0))


def downgrade():
    # Older releases ignore the added field; retain user edits on downgrade/re-upgrade.
    pass
