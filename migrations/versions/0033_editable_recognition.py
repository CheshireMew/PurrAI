"""Move remaining recognition vocabulary to the existing shared policy."""
import copy
import json

from alembic import op
import sqlalchemy as sa

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade():
    from importlib.resources import files

    seed = json.loads(files("purrai").joinpath("assets/recognition_policy.v1.json").read_text(encoding="utf-8"))
    table = sa.table("shared_policy", sa.column("id", sa.Integer()), sa.column("business_rules", sa.JSON()))
    connection = op.get_bind()
    for identifier, document in connection.execute(sa.select(table.c.id, table.c.business_rules)):
        if "recognition" not in document:
            document = {**copy.deepcopy(document), "recognition": copy.deepcopy(seed)}
            connection.execute(table.update().where(table.c.id == identifier).values(business_rules=document))


def downgrade():
    # Older releases ignore the additional document key. Preserve administrator edits.
    pass
