"""Private deployment: groups require the owner's explicit approval."""

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = depends_on = None


def upgrade():
    # Existing membership or administrator status does not prove the owner's consent.
    op.add_column("groups", sa.Column("approved", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade():
    raise RuntimeError("Automatic destructive downgrade is not supported; restore a backup.")
