"""Persist answer choices; existing digit keyboards remain valid."""

from alembic import op
import sqlalchemy as sa

revision = "0014"
down_revision = "0013"
branch_labels = depends_on = None


def upgrade():
    op.add_column("verifications", sa.Column("choices", sa.JSON(), nullable=False, server_default="[]"))


def downgrade():
    raise RuntimeError("Automatic destructive downgrade is not supported; restore a backup.")
