"""Persist verification method, prompt data and original update receipt time."""
from alembic import op
import sqlalchemy as sa
import json

revision = "0028"
down_revision = "0027"
branch_labels = depends_on = None


def upgrade():
    op.add_column("verifications", sa.Column("method", sa.String(), nullable=False, server_default="image"))
    op.add_column("verifications", sa.Column("challenge", sa.JSON(), nullable=False, server_default="{}"))
    op.add_column("inbox", sa.Column("received_at", sa.Float(), nullable=False, server_default="0"))
    op.execute("UPDATE inbox SET received_at = available_at")
    connection = op.get_bind()
    for name in ("groups", "shared_policy"):
        table = sa.table(name, sa.column("id"), sa.column("settings", sa.JSON()))
        for identifier, settings in connection.execute(sa.select(table.c.id, table.c.settings)):
            settings = json.loads(settings) if isinstance(settings, str) else dict(settings)
            settings.setdefault("verification_method", "image")
            settings.setdefault("verification_channel", "")
            connection.execute(table.update().where(table.c.id == identifier).values(settings=settings))


def downgrade():
    op.drop_column("inbox", "received_at")
    op.drop_column("verifications", "challenge")
    op.drop_column("verifications", "method")
