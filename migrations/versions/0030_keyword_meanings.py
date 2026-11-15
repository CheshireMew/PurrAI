"""Store editable keyword explanations without installing business vocabulary."""
from alembic import op
import sqlalchemy as sa

revision = "0030"
down_revision = "0029"
branch_labels = depends_on = None


def upgrade():
    op.add_column("rules", sa.Column("meaning", sa.String(), nullable=False, server_default=""))


def downgrade():
    op.drop_column("rules", "meaning")
