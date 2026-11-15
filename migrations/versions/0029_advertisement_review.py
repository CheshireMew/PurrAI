"""Keep advertisement reviews separate from conversational replies."""
from alembic import op
import sqlalchemy as sa

revision = "0029"
down_revision = "0028"
branch_labels = depends_on = None


def upgrade():
    op.add_column("chat_turns", sa.Column("review_only", sa.Boolean(), nullable=False, server_default="0"))


def downgrade():
    op.drop_column("chat_turns", "review_only")
