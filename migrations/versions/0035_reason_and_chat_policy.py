"""Expose stored-verdict recognition and remaining conversation windows."""
import copy
import json
from importlib.resources import files

from alembic import op
import sqlalchemy as sa

revision = "0035"
down_revision = "0034"
branch_labels = depends_on = None

SETTINGS = {"chat_message_max_age_seconds": 120, "chat_context_hours": 6,
            "chat_dialogue_seconds": 120, "chat_burst_seconds": 10}


def upgrade():
    conn = op.get_bind()
    if "reason_details" not in {c["name"] for c in sa.inspect(conn).get_columns("violations")}:
        op.add_column("violations", sa.Column("reason_details", sa.JSON(), nullable=False, server_default="{}"))
    groups = sa.table("groups", sa.column("id", sa.BigInteger()), sa.column("settings", sa.JSON()))
    for identifier, settings in conn.execute(sa.select(groups)):
        conn.execute(groups.update().where(groups.c.id == identifier).values(settings={**SETTINGS, **settings}))
    seed = json.loads(files("purrai").joinpath("assets/reason_policy.v1.json").read_text(encoding="utf-8"))
    policies = sa.table("shared_policy", sa.column("id", sa.Integer()), sa.column("settings", sa.JSON()),
                        sa.column("business_rules", sa.JSON()))
    for identifier, settings, document in conn.execute(sa.select(policies)):
        document = copy.deepcopy(document)
        document.setdefault("reason_policy", seed)
        conn.execute(policies.update().where(policies.c.id == identifier).values(
            settings={**SETTINGS, **settings}, business_rules=document))


def downgrade():
    # Preserve edits and explicit disposition evidence across rollback/re-upgrade.
    pass
