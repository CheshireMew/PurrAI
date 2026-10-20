"""Track membership epochs independently of Telegram's second-resolution timestamps."""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("members", sa.Column("present", sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column("members", sa.Column("last_event_id", sa.BigInteger(), nullable=False, server_default="0"))


def downgrade():
    raise RuntimeError("Automatic destructive downgrade is not supported; restore a backup.")
