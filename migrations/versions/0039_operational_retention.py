"""Indexes for bounded review history and source-reference retention."""

from alembic import op
import sqlalchemy as sa

revision = "0039"
down_revision = "0038"
branch_labels = depends_on = None


def upgrade():
    op.execute("CREATE INDEX IF NOT EXISTS ix_ad_review_retention ON advertisement_reviews(event_at, id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_ad_review_history ON advertisement_reviews(created_at, id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_ad_review_raw ON advertisement_reviews(event_at, id) WHERE json_extract(message, '$._retained') IS NOT 1")
    op.execute("CREATE INDEX IF NOT EXISTS ix_points_job_raw ON actions(created_at, id) WHERE kind = 'points_message' AND json_type(payload, '$.message') IS NOT NULL")
    op.execute("CREATE INDEX IF NOT EXISTS ix_action_review_source ON actions(json_extract(payload, '$.advertisement_evidence.review_id'))")
    table = op.create_table("advertisement_sample_tokens",
        sa.Column("sample_id", sa.String(), sa.ForeignKey("advertisement_samples.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("character", sa.String(), primary_key=True),
        sa.Column("group_id", sa.BigInteger(), nullable=False),
        sa.Column("occurrences", sa.Integer(), nullable=False), if_not_exists=True)
    op.execute("CREATE INDEX IF NOT EXISTS ix_ad_sample_character ON advertisement_sample_tokens(group_id, character, occurrences, sample_id)")
    from purrai.sample_index import postings
    connection = op.get_bind()
    table = sa.table("advertisement_sample_tokens", sa.column("sample_id"), sa.column("group_id"),
                     sa.column("character"), sa.column("occurrences"))
    cursor = ""
    while True:
        rows = connection.execute(sa.text("SELECT id, group_id, text FROM advertisement_samples WHERE id > :cursor ORDER BY id LIMIT 100"),
                                  {"cursor": cursor}).fetchall()
        if not rows:
            break
        values = [entry for row in rows for entry in postings(*row)]
        if values:
            from sqlalchemy.dialects.sqlite import insert
            connection.execute(insert(table).on_conflict_do_nothing(), values)
        cursor = rows[-1][0]


def downgrade():
    pass
