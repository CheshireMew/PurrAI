"""Durable per-message review and independently confirmed advertisement samples."""
from alembic import op
import sqlalchemy as sa

revision = "0032"
down_revision = "0031"
branch_labels = depends_on = None


def upgrade():
    op.create_table("advertisement_policies",
        sa.Column("group_id", sa.BigInteger(), sa.ForeignKey("groups.id"), primary_key=True),
        sa.Column("config", sa.JSON(), nullable=False), sa.Column("version", sa.Integer(), nullable=False))
    op.create_table("advertisement_samples",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("group_id", sa.BigInteger(), sa.ForeignKey("groups.id"), nullable=False),
        sa.Column("text", sa.Text(), nullable=False), sa.Column("sender_name", sa.String(), nullable=False),
        sa.Column("scope", sa.String(), nullable=False), sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("source", sa.String(), nullable=False), sa.Column("source_key", sa.String(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False), sa.Column("violation_id", sa.String(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False), sa.Column("actor_id", sa.BigInteger(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False), sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.Column("review_state", sa.String(), nullable=False), sa.Column("review_attempts", sa.Integer(), nullable=False),
        sa.Column("review_after", sa.Float(), nullable=False), sa.Column("review_error", sa.Text(), nullable=False),
        sa.UniqueConstraint("group_id", "source_key"))
    op.create_index("ix_advertisement_samples_group_id", "advertisement_samples", ["group_id"])
    op.create_table("advertisement_reviews",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("group_id", sa.BigInteger(), sa.ForeignKey("groups.id"), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False), sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("revision", sa.String(), nullable=False), sa.Column("message", sa.JSON(), nullable=False),
        sa.Column("event_at", sa.Float(), nullable=False), sa.Column("update_id", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.String(), nullable=False), sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False), sa.Column("available_at", sa.Float(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False), sa.Column("updated_at", sa.Float(), nullable=False),
        sa.Column("error", sa.Text(), nullable=False), sa.UniqueConstraint("group_id", "message_id", "revision"))
    op.create_index("ix_ad_review_queue", "advertisement_reviews", ["state", "available_at"])
    op.create_index("ix_ad_review_message", "advertisement_reviews", ["group_id", "message_id"])


def downgrade():
    op.drop_table("advertisement_reviews")
    op.drop_table("advertisement_samples")
    op.drop_table("advertisement_policies")
