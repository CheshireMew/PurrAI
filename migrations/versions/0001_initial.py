"""Frozen initial SQLite schema; independent of future application models."""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

DDL = [
    "CREATE TABLE actions (\n\tid VARCHAR NOT NULL, \n\tdedupe_key VARCHAR NOT NULL, \n\tgroup_id BIGINT, \n\tuser_id BIGINT, \n\tkind VARCHAR NOT NULL, \n\tpayload JSON NOT NULL, \n\tstate VARCHAR NOT NULL, \n\tpriority INTEGER NOT NULL, \n\tattempts INTEGER NOT NULL, \n\tavailable_at FLOAT NOT NULL, \n\terror TEXT NOT NULL, \n\tactor_id BIGINT, \n\treason VARCHAR NOT NULL, \n\tcreated_at FLOAT NOT NULL, \n\tupdated_at FLOAT NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (dedupe_key)\n)",
    "CREATE TABLE admins (\n\tid BIGINT NOT NULL, \n\tname VARCHAR NOT NULL, \n\tactive BOOLEAN NOT NULL, \n\tPRIMARY KEY (id)\n)",
    "CREATE TABLE fingerprints (\n\tgroup_id BIGINT NOT NULL, \n\tuser_id BIGINT NOT NULL, \n\tmessage_id BIGINT NOT NULL, \n\tdigest VARCHAR NOT NULL, \n\tcreated_at FLOAT NOT NULL, \n\tPRIMARY KEY (group_id, user_id, message_id)\n)",
    "CREATE TABLE groups (\n\tid BIGINT NOT NULL, \n\ttitle VARCHAR NOT NULL, \n\ttype VARCHAR NOT NULL, \n\tenabled BOOLEAN NOT NULL, \n\tstatus VARCHAR NOT NULL, \n\tpermissions JSON NOT NULL, \n\tsettings JSON NOT NULL, \n\tactivated_at FLOAT NOT NULL, \n\tcreated_at FLOAT NOT NULL, \n\tPRIMARY KEY (id)\n)",
    "CREATE TABLE inbox (\n\tid BIGINT NOT NULL, \n\tpayload JSON NOT NULL, \n\tstate VARCHAR NOT NULL, \n\tattempts INTEGER NOT NULL, \n\tavailable_at FLOAT NOT NULL, \n\terror TEXT NOT NULL, \n\tPRIMARY KEY (id)\n)",
    "CREATE TABLE login_attempts (\n\tstate VARCHAR NOT NULL, \n\tnonce VARCHAR NOT NULL, \n\tverifier VARCHAR NOT NULL, \n\texpires_at FLOAT NOT NULL, \n\tPRIMARY KEY (state)\n)",
    'CREATE TABLE meta (\n\t"key" VARCHAR NOT NULL, \n\tvalue TEXT NOT NULL, \n\tPRIMARY KEY ("key")\n)',
    "CREATE TABLE sessions (\n\tid VARCHAR NOT NULL, \n\tuser_id BIGINT NOT NULL, \n\tcsrf VARCHAR NOT NULL, \n\texpires_at FLOAT NOT NULL, \n\tPRIMARY KEY (id)\n)",
    "CREATE TABLE grants (\n\tadmin_id BIGINT NOT NULL, \n\tgroup_id BIGINT NOT NULL, \n\tPRIMARY KEY (admin_id, group_id), \n\tFOREIGN KEY(admin_id) REFERENCES admins (id), \n\tFOREIGN KEY(group_id) REFERENCES groups (id)\n)",
    "CREATE TABLE members (\n\tgroup_id BIGINT NOT NULL, \n\tuser_id BIGINT NOT NULL, \n\tjoined_at FLOAT NOT NULL, \n\tpassed_at FLOAT NOT NULL, \n\tgeneration VARCHAR NOT NULL, \n\towned_restriction JSON, \n\trestriction_conflict BOOLEAN NOT NULL, \n\tPRIMARY KEY (group_id, user_id), \n\tFOREIGN KEY(group_id) REFERENCES groups (id)\n)",
    "CREATE TABLE rules (\n\tid VARCHAR NOT NULL, \n\tgroup_id BIGINT NOT NULL, \n\tkind VARCHAR NOT NULL, \n\tvalue VARCHAR NOT NULL, \n\tmode VARCHAR NOT NULL, \n\tenabled BOOLEAN NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(group_id) REFERENCES groups (id)\n)",
    "CREATE TABLE verifications (\n\tid VARCHAR NOT NULL, \n\tgroup_id BIGINT NOT NULL, \n\tuser_id BIGINT NOT NULL, \n\tuser_name VARCHAR NOT NULL, \n\tgeneration VARCHAR NOT NULL, \n\tstate VARCHAR NOT NULL, \n\tnonce VARCHAR NOT NULL, \n\tanswer_hash VARCHAR NOT NULL, \n\tinput_digits VARCHAR NOT NULL, \n\tattempts INTEGER NOT NULL, \n\tmessage_id BIGINT, \n\tdeadline FLOAT NOT NULL, \n\tprevious_permissions JSON NOT NULL, \n\terror TEXT NOT NULL, \n\tcreated_at FLOAT NOT NULL, \n\tupdated_at FLOAT NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(group_id) REFERENCES groups (id)\n)",
    "CREATE TABLE violations (\n\tid VARCHAR NOT NULL, \n\tgroup_id BIGINT NOT NULL, \n\tuser_id BIGINT NOT NULL, \n\tmessage_id BIGINT NOT NULL, \n\tsender_chat BOOLEAN NOT NULL, \n\texcerpt TEXT NOT NULL, \n\treasons JSON NOT NULL, \n\twarning_active BOOLEAN NOT NULL, \n\tcreated_at FLOAT NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(group_id) REFERENCES groups (id), \n\tUNIQUE (group_id, message_id)\n)",
    "CREATE TABLE whitelist (\n\tgroup_id BIGINT NOT NULL, \n\tuser_id BIGINT NOT NULL, \n\tverification BOOLEAN NOT NULL, \n\tmoderation BOOLEAN NOT NULL, \n\tnote VARCHAR NOT NULL, \n\tPRIMARY KEY (group_id, user_id), \n\tFOREIGN KEY(group_id) REFERENCES groups (id)\n)",
    "CREATE INDEX ix_actions_group_id ON actions (group_id)",
    "CREATE INDEX ix_actions_state ON actions (state)",
    "CREATE INDEX ix_fingerprints_created_at ON fingerprints (created_at)",
    "CREATE INDEX ix_fingerprints_digest ON fingerprints (digest)",
    "CREATE INDEX ix_inbox_state ON inbox (state)",
    "CREATE INDEX ix_sessions_expires_at ON sessions (expires_at)",
    "CREATE INDEX ix_rules_group_id ON rules (group_id)",
    "CREATE INDEX ix_verifications_group_id ON verifications (group_id)",
    "CREATE INDEX ix_verifications_state ON verifications (state)",
    "CREATE INDEX ix_verifications_user_id ON verifications (user_id)",
    "CREATE INDEX ix_violations_group_id ON violations (group_id)",
    "CREATE INDEX ix_violations_user_id ON violations (user_id)",
]


def upgrade():
    for statement in DDL:
        op.execute(statement)


def downgrade():
    raise RuntimeError("Automatic destructive downgrade is not supported; restore a backup.")
