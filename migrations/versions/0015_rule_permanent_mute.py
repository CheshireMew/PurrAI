"""Allow selected advertising rules to delete and permanently mute."""

from alembic import op
import sqlalchemy as sa

revision = "0015"
down_revision = "0014"
branch_labels = depends_on = None


def upgrade():
    op.add_column("rules", sa.Column("penalty", sa.String(), nullable=False, server_default="delete"))
    op.add_column("violations", sa.Column("rule_mute", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade():
    raise RuntimeError("Automatic destructive downgrade is not supported; restore a backup.")
