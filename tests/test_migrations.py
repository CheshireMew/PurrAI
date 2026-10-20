import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

from purrai.models import default_settings


def test_fresh_install_upgrade_backup_restore_preserves_members_and_tasks(tmp_path):
    root = Path(__file__).resolve().parents[1]
    database = tmp_path / "migrating.sqlite3"
    backup_dir = tmp_path / "backups"
    env = {
        **os.environ,
        "PURRAI_DATABASE_URL": f"sqlite+aiosqlite:///{database}",
        "PURRAI_BACKUP_DIR": str(backup_dir),
    }

    def run(*arguments):
        result = subprocess.run(
            [sys.executable, *arguments],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=40,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout

    run("-m", "alembic", "upgrade", "0001")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO groups VALUES (?,?,?,?,?,?,?,?,?)",
            (
                -1001,
                "迁移保留群",
                "supergroup",
                True,
                "active",
                "{}",
                json.dumps(
                    {
                        key: value
                        for key, value in {**default_settings(), "mute_seconds": 1800}.items()
                        if key
                        not in (
                            "join_requests",
                            "verification_failure_action",
                            "newcomer_messages",
                            "media_min_text_messages",
                            "emoji_status_blocked_packs",
                            "newcomer_mentions",
                            "similar_enabled",
                            "similar_window",
                            "similar_count",
                            "similar_threshold",
                            "similar_min_length",
                            "burst_enabled",
                            "burst_window",
                            "burst_count",
                            "chat_stickers",
                            "chat_sticker_packs",
                            "chat_sticker_interval",
                            "chat_antifraud",
                            "chat_antifraud_packs",
                            "chat_antifraud_interval",
                        )
                    }
                ),
                0,
                1,
            ),
        )
        connection.execute(
            "INSERT INTO members VALUES (?,?,?,?,?,?,?)", (-1001, 42, 1, 0, "join-1", None, False)
        )
        connection.execute(
            "INSERT INTO actions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "task-1",
                "pending-delete",
                -1001,
                42,
                "delete",
                '{"message_id":100}',
                "pending",
                0,
                0,
                1,
                "",
                None,
                "",
                1,
                1,
            ),
        )
        connection.execute(
            "INSERT INTO violations VALUES (?,?,?,?,?,?,?,?,?)",
            ("legacy-ad", -1001, 42, 100, False, "旧广告摘录", '["旧关键词"]', False, 1),
        )
    run("-m", "alembic", "upgrade", "0004")
    with sqlite3.connect(database) as connection:
        verification = {
            "id": "old-failure",
            "group_id": -1001,
            "user_id": 42,
            "user_name": "旧验证",
            "generation": "join-1",
            "state": "removing",
            "nonce": "old-nonce",
            "answer_hash": "",
            "input_digits": "",
            "attempts": 3,
            "message_id": None,
            "deadline": 1,
            "previous_permissions": "{}",
            "error": "",
            "created_at": 1,
            "updated_at": 1,
            "flow": "joined",
            "prompt_chat_id": None,
            "request_date": 0,
        }
        names = list(verification)
        connection.execute(
            "INSERT INTO verifications ("
            + ",".join(names)
            + ") VALUES ("
            + ",".join("?" for _ in names)
            + ")",
            [verification[key] for key in names],
        )
        payload = json.dumps({"verification_id": "old-failure", "expected_nonce": "old-nonce"})
        for identifier, actor in (("old-auto-ban", None), ("old-manual-ban", 1)):
            connection.execute(
                "INSERT INTO actions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    identifier,
                    identifier,
                    -1001,
                    42,
                    "ban",
                    payload,
                    "pending",
                    0,
                    0,
                    1,
                    "",
                    actor,
                    "旧失败",
                    1,
                    1,
                ),
            )
    output = run("-m", "purrai.cli", "init-db")
    assert "备份" in output
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0014"
        assert json.loads(connection.execute("SELECT choices FROM verifications").fetchone()[0]) == []
        assert connection.execute("SELECT join_observed FROM members").fetchone()[0] == 1
        assert connection.execute("SELECT approved FROM groups").fetchone()[0] == 0
        assert connection.execute("SELECT present,last_event_id FROM members").fetchone() == (1, 0)
        assert connection.execute("SELECT observed_messages FROM members").fetchone()[0] == 1000000
        settings = json.loads(connection.execute("SELECT settings FROM groups").fetchone()[0])
        assert settings["mute_seconds"] == 259200 and settings["newcomer_messages"] == 3
        assert settings["join_requests"] is False
        assert settings["archive_enabled"] is True
        assert settings["verification_failure_action"] == "permanent_mute"
        assert settings["media_min_text_messages"] == 3
        assert settings["emoji_status_blocked_packs"] == ["bygbb666", "gshechaba"]
        assert connection.execute("SELECT count(*) FROM member_texts").fetchone()[0] == 0
        assert settings["chat_enabled"] is False and settings["chat_history"] == 40
        assert settings["chat_name"] == "柴郡喵"
        assert settings["chat_sticker_interval"] == 60 and len(settings["chat_sticker_packs"]) == 20
        assert settings["chat_antifraud_packs"] == ["FTX888"] and settings["chat_antifraud"]
        assert connection.execute("SELECT COUNT(*) FROM chat_messages").fetchone()[0] == 0
        assert (
            connection.execute("SELECT kind FROM actions WHERE id='old-auto-ban'").fetchone()[0]
            == "verification_failure"
        )
        assert connection.execute("SELECT kind FROM actions WHERE id='old-manual-ban'").fetchone()[0] == "ban"
        assert (
            connection.execute("SELECT state FROM verifications WHERE id='old-failure'").fetchone()[0]
            == "failing"
        )
        assert connection.execute(
            "SELECT text,source,original_observed,deletion_state FROM archived_messages"
        ).fetchone() == ("旧广告摘录", "violation_excerpt", 0, "pending")
        payload = json.loads(connection.execute("SELECT payload FROM message_revisions").fetchone()[0])
        assert payload["partial"] and payload["sent_at_unknown"]
        assert connection.execute("SELECT state FROM actions").fetchone()[0] == "pending"
    snapshot = next(backup_dir.glob("*.sqlite3"))
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE groups SET title='后续修改'")
    run("-m", "purrai.cli", "restore", str(snapshot))
    run("-m", "purrai.cli", "init-db")
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT title FROM groups").fetchone()[0] == "迁移保留群"
        assert connection.execute("SELECT state FROM actions").fetchone()[0] == "pending"
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert len(list(backup_dir.glob("*.sqlite3"))) >= 3


def test_additional_status_pack_appends_without_replacing_existing_rules(tmp_path, monkeypatch):
    import importlib.util
    import sqlalchemy as sa

    script = Path(__file__).resolve().parents[1] / "migrations/versions/0012_add_status_emoji_pack.py"
    spec = importlib.util.spec_from_file_location("additional_status_pack", script)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'status-packs.sqlite3'}")
    original = [
        {"emoji_status_blocked_packs": ["bygbb666"]},
        {"emoji_status_blocked_packs": ["custom_pack"]},
        {"emoji_status_blocked_packs": []},
        {"emoji_status_blocked_packs": ["GsHeChaba", "custom_pack"]},
        {},
    ]
    expected = [
        ["bygbb666", "gshechaba"],
        ["custom_pack", "gshechaba"],
        ["gshechaba"],
        ["GsHeChaba", "custom_pack"],
        ["bygbb666", "gshechaba"],
    ]
    try:
        with engine.begin() as connection:
            connection.execute(
                sa.text("CREATE TABLE groups (id INTEGER PRIMARY KEY, settings TEXT NOT NULL)")
            )
            for identifier, settings in enumerate(original):
                connection.execute(
                    sa.text("INSERT INTO groups VALUES (:id,:settings)"),
                    {"id": identifier, "settings": json.dumps({**settings, "repeat_count": 9})},
                )
            monkeypatch.setattr(migration.op, "get_bind", lambda: connection)
            migration.upgrade()
            migration.upgrade()
            rows = connection.execute(sa.text("SELECT settings FROM groups ORDER BY id")).scalars().all()
            for saved, packs in zip(rows, expected, strict=True):
                assert json.loads(saved) == {"emoji_status_blocked_packs": packs, "repeat_count": 9}
    finally:
        engine.dispose()


def test_join_evidence_migration_keeps_unknown_and_manual_records_exempt(tmp_path, monkeypatch):
    import importlib.util
    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    script = Path(__file__).resolve().parents[1] / "migrations/versions/0013_observed_member_join.py"
    spec = importlib.util.spec_from_file_location("observed_join", script)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'join-evidence.sqlite3'}")
    try:
        with engine.begin() as connection:
            connection.execute(sa.text("CREATE TABLE members (group_id INTEGER, user_id INTEGER, joined_at FLOAT, generation TEXT)"))
            connection.execute(sa.text("CREATE TABLE verifications (group_id INTEGER, user_id INTEGER, generation TEXT, flow TEXT, previous_permissions JSON)"))
            for identifier in range(1, 7):
                connection.execute(sa.text("INSERT INTO members VALUES (-1001,:id,:joined,'current')"),
                                   {"id": identifier, "joined": 0 if identifier == 6 else 100})
            for identifier, generation, flow, permissions in [
                (2, "current", "joined", {}),
                (3, "current", "join_request", {"entry_consumed": True}),
                (4, "current", "join_request", {}),
                (5, "prior", "joined", {}),
                (6, "current", "joined", {}),
            ]:
                connection.execute(sa.text("INSERT INTO verifications VALUES (-1001,:id,:generation,:flow,:permissions)"),
                                   {"id": identifier, "generation": generation, "flow": flow,
                                    "permissions": json.dumps(permissions)})
            monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
            migration.upgrade()
            assert connection.execute(sa.text("SELECT join_observed FROM members ORDER BY user_id")).scalars().all() == [0, 1, 1, 0, 0, 0]
    finally:
        engine.dispose()
