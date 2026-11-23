"""Group lotteries and durable participation / prize receipts."""
from alembic import op
import sqlalchemy as sa

revision = "0037"
down_revision = "0036"
branch_labels = depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if "administrator_backup" not in {column["name"] for column in inspector.get_columns("whitelist")}:
        op.add_column("whitelist", sa.Column("administrator_backup", sa.JSON(), nullable=True))
    lotteries = op.create_table("lotteries",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("group_id", sa.BigInteger(), sa.ForeignKey("groups.id"), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("participant_count", sa.Integer(), nullable=False),
        sa.Column("draw_count", sa.Integer(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("opened_at", sa.Float(), nullable=False),
        sa.Column("finished_at", sa.Float(), nullable=False), if_not_exists=True)
    op.create_index("ix_lotteries_group_id", "lotteries", ["group_id"], if_not_exists=True)
    op.create_index("ix_lotteries_state", "lotteries", ["state"], if_not_exists=True)
    entries = op.create_table("lottery_entries",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("lottery_id", sa.String(), sa.ForeignKey("lotteries.id"), nullable=False),
        sa.Column("group_id", sa.BigInteger(), sa.ForeignKey("groups.id"), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("display_name", sa.String(), nullable=False),
        sa.Column("request_id", sa.String(), nullable=False),
        sa.Column("day", sa.String(), nullable=False),
        sa.Column("fee", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("prize_index", sa.Integer(), nullable=True),
        sa.Column("prize_label", sa.String(), nullable=False),
        sa.Column("reward_points", sa.BigInteger(), nullable=False),
        sa.Column("fulfilled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.UniqueConstraint("lottery_id", "request_id"), if_not_exists=True)
    inspector = sa.inspect(op.get_bind())
    for table in (lotteries, entries):
        actual = {column["name"]: column for column in inspector.get_columns(table.name)}
        if set(actual) != set(table.c.keys()) or any(
            actual[column.name]["type"]._type_affinity != column.type._type_affinity
            or actual[column.name]["nullable"] != column.nullable for column in table.c
        ):
            raise RuntimeError(f"Existing {table.name} schema is incompatible; preserved data was not modified")
    for name in ("lottery_id", "group_id", "state"):
        op.create_index(f"ix_lottery_entries_{name}", "lottery_entries", [name], if_not_exists=True)
    op.create_index("ix_lottery_entry_user", "lottery_entries", ["lottery_id", "user_id", "day"], if_not_exists=True)


def downgrade():
    # Prize and fee receipts must not disappear during an application rollback.
    pass
