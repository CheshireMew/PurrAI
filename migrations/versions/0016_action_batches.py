"""Index batch membership so a batch needs one progress request."""

import json

from alembic import op
import sqlalchemy as sa

revision = "0016"
down_revision = "0015"
branch_labels = depends_on = None


def upgrade():
    op.add_column("actions", sa.Column("batch_id", sa.String(), nullable=True))
    connection = op.get_bind()
    batches = connection.execute(sa.text("SELECT id,payload FROM actions WHERE kind='batch_release'"))
    for identifier, payload in batches:
        ids = json.loads(payload).get("action_ids", [])
        connection.execute(sa.text(
            "UPDATE actions SET batch_id=:batch WHERE id IN (SELECT value FROM json_each(:ids))"
        ), {"batch": identifier, "ids": json.dumps(ids)})
    op.create_index("ix_actions_batch_state", "actions", ["batch_id", "state"])


def downgrade():
    raise RuntimeError("Automatic destructive downgrade is not supported; restore a backup.")
