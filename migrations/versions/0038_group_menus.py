"""Editable group reply keyboards; existing settings and ledgers remain unchanged."""
from alembic import op
import sqlalchemy as sa

revision = "0038"
down_revision = "0037"
branch_labels = depends_on = None


def upgrade():
    op.create_table("group_menus",
        sa.Column("group_id", sa.BigInteger(), sa.ForeignKey("groups.id"), primary_key=True),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("active_config", sa.JSON(), nullable=True),
        sa.Column("last_action_id", sa.String(), nullable=False), if_not_exists=True)


def downgrade():
    # Preserve administrator configuration and delivery state on rollback.
    pass
