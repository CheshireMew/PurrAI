"""Move the legacy business policy into an independently editable database copy."""

from alembic import op
import sqlalchemy as sa
import json
from importlib.resources import files

revision = "0026"
down_revision = "0025"
branch_labels = depends_on = None


def upgrade():
    op.add_column("shared_policy", sa.Column("business_rules", sa.JSON(), nullable=True))
    table = sa.table("shared_policy", sa.column("id", sa.Integer()), sa.column("business_rules", sa.JSON()))
    seed = json.loads(files("purrai").joinpath("assets/business_rules.v1.json").read_text(encoding="utf-8"))
    op.get_bind().execute(table.update().values(business_rules=seed))
    with op.batch_alter_table("shared_policy") as batch:
        batch.alter_column("business_rules", existing_type=sa.JSON(), nullable=False)
    # An absent singleton uses the same seed when first created; no group settings,
    # custom rules or learned promotion targets are overwritten.


def downgrade():
    with op.batch_alter_table("shared_policy") as batch:
        batch.drop_column("business_rules")
