"""Shared defaults, explicit local overrides and the requested status emoji pack."""

import json

from alembic import op
import sqlalchemy as sa

revision = "0022"
down_revision = "0021"
branch_labels = depends_on = None


def upgrade():
    from purrai.schemas import GroupSettings

    op.add_column("groups", sa.Column("setting_overrides", sa.JSON(), nullable=False, server_default="[]"))
    op.add_column("rules", sa.Column("inherited_from", sa.String(), nullable=False, server_default=""))
    op.add_column("rules", sa.Column("customized", sa.Boolean(), nullable=False, server_default="1"))
    op.create_table("shared_policy", sa.Column("id", sa.Integer(), primary_key=True),
                    sa.Column("settings", sa.JSON(), nullable=False), sa.Column("rules", sa.JSON(), nullable=False))
    connection = op.get_bind()
    defaults = GroupSettings().model_dump()
    # Preserve every existing difference; equal historical administrator writes
    # are recovered from their audit results where available.
    explicit = {}
    for row in connection.execute(sa.text("SELECT group_id,reason,payload FROM actions WHERE kind='settings' AND state='succeeded'")).mappings():
        reason = row["reason"] or ""
        if reason.startswith("Telegram 修改配置："):
            explicit.setdefault(row["group_id"], set()).add(reason.partition("：")[2])
    for row in connection.execute(sa.text("SELECT id,settings FROM groups")).mappings().all():
        settings = json.loads(row["settings"])
        packs = settings.get("emoji_status_blocked_packs", ["bygbb666", "gshechaba"])
        if not any(pack.lower() == "tgvip_16" for pack in packs):
            settings["emoji_status_blocked_packs"] = [*packs, "tgvip_16"]
        overrides = {key for key, value in settings.items() if key in defaults and value != defaults[key]}
        overrides |= explicit.get(row["id"], set()) & defaults.keys()
        settings = {**defaults, **settings}
        connection.execute(sa.text("UPDATE groups SET settings=:settings,setting_overrides=:overrides WHERE id=:id"),
                           {"id": row["id"], "settings": json.dumps(settings, ensure_ascii=False),
                            "overrides": json.dumps(sorted(overrides))})
    # Rules present in every configured rule list are common. Empty lists mean
    # that the group has not configured rules yet. Other local rules stay local.
    fields = ("kind", "value", "mode", "enabled", "scope", "context_value", "penalty")
    rows = connection.execute(sa.text("SELECT * FROM rules ORDER BY id")).mappings().all()
    by_group = {}
    for row in rows:
        by_group.setdefault(row["group_id"], set()).add(tuple(row[key] for key in fields))
    shared = set.intersection(*by_group.values()) if by_group else set()
    common, identifiers = [], {}
    for row in rows:
        signature = tuple(row[key] for key in fields)
        if signature not in shared:
            continue
        if signature not in identifiers:
            identifiers[signature] = row["id"]
            common.append({"id": row["id"], **{key: row[key] for key in fields}})
        connection.execute(sa.text("UPDATE rules SET inherited_from=:origin,customized=0 WHERE id=:id"),
                           {"origin": identifiers[signature], "id": row["id"]})
    connection.execute(sa.text("INSERT INTO shared_policy (id,settings,rules) VALUES (1,:settings,:rules)"),
                       {"settings": json.dumps(defaults, ensure_ascii=False), "rules": json.dumps(common, ensure_ascii=False)})
    # Pending checks will use the new behavior; pending message penalties have
    # not run yet and must follow the requested permanent mute behavior too.
    connection.execute(sa.text("UPDATE actions SET kind='emoji_status_mute' WHERE kind='emoji_status_ban' AND state='pending' "
                               "AND json_array_length(json_extract(payload,'$.message_ids')) > 0"))


def downgrade():
    raise RuntimeError("Automatic destructive downgrade is not supported; restore a backup.")
