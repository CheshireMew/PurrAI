"""Store per-group keyword replies without changing moderation rules."""

from alembic import op
import sqlalchemy as sa

revision = "0027"
down_revision = "0026"
branch_labels = depends_on = None


def upgrade():
    op.create_table("auto_replies",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("group_id", sa.BigInteger(), sa.ForeignKey("groups.id"), nullable=False),
        sa.Column("keyword", sa.String(), nullable=False),
        sa.Column("reply", sa.Text(), nullable=False),
        sa.Column("mode", sa.String(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("cooldown_seconds", sa.Integer(), nullable=False),
        sa.Column("last_triggered_at", sa.Float(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False))
    op.create_index("ix_auto_replies_group_id", "auto_replies", ["group_id"])


def downgrade():
    op.drop_table("auto_replies")
