"""Group points accounts, import receipts and immutable ledger entries."""
from alembic import op
import sqlalchemy as sa

revision = "0031"
down_revision = "0030"
branch_labels = depends_on = None


def upgrade():
    op.create_table("points_accounts",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("group_id", sa.BigInteger(), sa.ForeignKey("groups.id"), nullable=False),
        sa.Column("source_key", sa.String(), nullable=False),
        sa.Column("display_name", sa.String(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("balance", sa.BigInteger(), nullable=False),
        sa.Column("source_rank", sa.Integer(), nullable=True),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.UniqueConstraint("group_id", "source_key"), sa.UniqueConstraint("group_id", "user_id"),
        sa.CheckConstraint("balance >= 0 AND balance <= 9007199254740991"))
    op.create_index("ix_points_accounts_group_id", "points_accounts", ["group_id"])
    op.create_table("points_imports",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("group_id", sa.BigInteger(), sa.ForeignKey("groups.id"), nullable=False),
        sa.Column("batch_key", sa.String(), nullable=False), sa.Column("digest", sa.String(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False), sa.Column("count", sa.Integer(), nullable=False),
        sa.Column("total_points", sa.BigInteger(), nullable=False),
        sa.Column("actor_id", sa.BigInteger(), nullable=False), sa.Column("created_at", sa.Float(), nullable=False),
        sa.UniqueConstraint("group_id", "batch_key"))
    op.create_index("ix_points_imports_group_id", "points_imports", ["group_id"])
    op.create_table("points_entries",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("group_id", sa.BigInteger(), sa.ForeignKey("groups.id"), nullable=False),
        sa.Column("account_id", sa.String(), sa.ForeignKey("points_accounts.id"), nullable=False),
        sa.Column("kind", sa.String(), nullable=False), sa.Column("delta", sa.BigInteger(), nullable=False),
        sa.Column("balance_after", sa.BigInteger(), nullable=False), sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor_id", sa.BigInteger(), nullable=False), sa.Column("request_id", sa.String(), nullable=False),
        sa.Column("intent", sa.JSON(), nullable=False), sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False), sa.UniqueConstraint("group_id", "request_id"))
    op.create_index("ix_points_entries_group_id", "points_entries", ["group_id"])
    op.create_index("ix_points_entries_account_id", "points_entries", ["account_id"])
    op.create_table("points_policies",
        sa.Column("group_id", sa.BigInteger(), sa.ForeignKey("groups.id"), primary_key=True),
        sa.Column("config", sa.JSON(), nullable=False), sa.Column("version", sa.Integer(), nullable=False))
    op.create_table("points_daily",
        sa.Column("group_id", sa.BigInteger(), sa.ForeignKey("groups.id"), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), primary_key=True), sa.Column("day", sa.String(), primary_key=True),
        sa.Column("signed", sa.Boolean(), nullable=False), sa.Column("chat_points", sa.Integer(), nullable=False),
        sa.Column("last_chat_at", sa.Float(), nullable=False), sa.Column("last_digest", sa.String(), nullable=False))
    op.create_table("points_packets",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("group_id", sa.BigInteger(), sa.ForeignKey("groups.id"), nullable=False),
        sa.Column("sender_id", sa.BigInteger(), nullable=False), sa.Column("target_id", sa.BigInteger(), nullable=True),
        sa.Column("message_id", sa.BigInteger(), nullable=False), sa.Column("total", sa.BigInteger(), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False), sa.Column("remaining", sa.BigInteger(), nullable=False),
        sa.Column("remaining_count", sa.Integer(), nullable=False), sa.Column("expires_at", sa.Float(), nullable=False),
        sa.Column("refunded", sa.Boolean(), nullable=False), sa.Column("created_at", sa.Float(), nullable=False),
        sa.UniqueConstraint("group_id", "message_id"), sa.CheckConstraint("remaining >= 0 AND remaining_count >= 0"))
    op.create_index("ix_points_packets_group_id", "points_packets", ["group_id"])
    op.create_index("ix_points_packets_expires_at", "points_packets", ["expires_at"])
    op.create_table("points_claims",
        sa.Column("packet_id", sa.String(), sa.ForeignKey("points_packets.id"), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), primary_key=True), sa.Column("points", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False))


def downgrade():
    op.drop_table("points_claims")
    op.drop_table("points_packets")
    op.drop_table("points_daily")
    op.drop_table("points_policies")
    op.drop_table("points_entries")
    op.drop_table("points_imports")
    op.drop_table("points_accounts")
