"""Distinguish observed joins from records created by manual member operations."""

from alembic import op
import sqlalchemy as sa

revision = "0013"
down_revision = "0012"
branch_labels = depends_on = None


def upgrade():
    op.add_column("members", sa.Column("join_observed", sa.Boolean(), nullable=False, server_default=sa.false()))
    # A membership record alone is not evidence of entry. Legacy verification
    # flows provide proof only for the same membership generation.
    op.execute(sa.text("""
        UPDATE members SET join_observed=1
        WHERE joined_at > 0 AND EXISTS (
            SELECT 1 FROM verifications v
            WHERE v.group_id=members.group_id AND v.user_id=members.user_id
              AND v.generation=members.generation
              AND (v.flow='joined' OR (
                  v.flow='join_request'
                  AND json_extract(v.previous_permissions, '$.entry_consumed')=1
              ))
        )
    """))


def downgrade():
    raise RuntimeError("Automatic destructive downgrade is not supported; restore a backup.")
