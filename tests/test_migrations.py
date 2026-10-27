import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

from sqlalchemy import create_engine, insert

from purrai.models import Action, ArchivedMessage, Fingerprint, Group, Member, MemberText, Verification, default_settings
from purrai.db import schema_head


def test_member_decision_migration_preserves_receipts_and_fences_obsolete_work(tmp_path):
    root = Path(__file__).resolve().parents[1]
    database = tmp_path / "decisions.sqlite3"
    environment = {**os.environ, "PURRAI_DATABASE_URL": f"sqlite+aiosqlite:///{database}",
                   "PURRAI_BACKUP_DIR": str(tmp_path / "backups")}

    def run(*arguments):
        result = subprocess.run([sys.executable, *arguments], cwd=root, env=environment,
                                capture_output=True, text=True, encoding="utf-8", timeout=40)
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout

    run("-m", "alembic", "upgrade", "0019")
    engine = create_engine(f"sqlite:///{database}")
    with engine.begin() as connection:
        connection.execute(insert(Group).values(id=-1001, approved=True, enabled=True))
        connection.execute(insert(Member), [
            {"group_id": -1001, "user_id": 44, "generation": "new-join", "owned_restriction": None},
            {"group_id": -1001, "user_id": 45, "generation": "same-join",
             "owned_restriction": {"kind": "profile", "source_action": "owned-profile"}},
        ])
        for identifier, user, kind, actor, state, created in [
            ("old-mute", 42, "manual_mute", 1, "pending", 1),
            ("latest-unmute", 42, "unmute", 2, "succeeded", 2),
            ("failed-older", 42, "manual_ban", 1, "failed", 0),
            ("other-member", 43, "manual_ban", 1, "pending", 1),
            ("unbound-profile", 44, "profile_mute", None, "pending", 1),
            ("owned-profile", 45, "profile_mute", None, "pending", 1),
        ]:
            connection.execute(insert(Action).values(
                id=identifier, dedupe_key=identifier, group_id=-1001, user_id=user,
                kind=kind, actor_id=actor, state=state, created_at=created, error="原始回执",
                payload={"message_id": 123} if kind == "profile_mute" else {},
            ))
        connection.execute(insert(ArchivedMessage).values(
            id="preserved", group_id=-1001, message_id=321, sent_at=1, text="保留的原始消息",
        ))
    engine.dispose()
    assert "备份" in run("-m", "purrai.cli", "init-db")
    with sqlite3.connect(database) as connection:
        receipts = dict(connection.execute("SELECT id,state FROM actions"))
        assert receipts == {"old-mute": "cancelled", "latest-unmute": "succeeded", "failed-older": "failed",
                            "other-member": "pending", "unbound-profile": "cancelled", "owned-profile": "pending"}
        assert connection.execute("SELECT value FROM meta WHERE key='member-decision:-1001:42'").fetchone()[0] == "latest-unmute"
        assert connection.execute("SELECT error FROM actions WHERE id='failed-older'").fetchone()[0] == "原始回执"
        bound = json.loads(connection.execute("SELECT payload FROM actions WHERE id='owned-profile'").fetchone()[0])
        assert bound["generation"] == "same-join" and bound["message_id"] == 123
        assert connection.execute("SELECT text FROM archived_messages WHERE id='preserved'").fetchone()[0] == "保留的原始消息"
        plan = [row[-1] for row in connection.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM actions WHERE group_id=-1001 AND kind='delete' "
            "AND json_extract(payload,'$.message_id')=123"
        )]
        assert any("ix_actions_message" in row and "<expr>=" in row for row in plan)
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_moderation_retention_migration_preserves_credit_history_and_recovers_stranded_cycles(tmp_path):
    root = Path(__file__).resolve().parents[1]
    database = tmp_path / "bounded.sqlite3"
    environment = {**os.environ, "PURRAI_DATABASE_URL": f"sqlite+aiosqlite:///{database}",
                   "PURRAI_BACKUP_DIR": str(tmp_path / "backups")}

    def run(*arguments):
        result = subprocess.run([sys.executable, *arguments], cwd=root, env=environment,
                                capture_output=True, text=True, encoding="utf-8", timeout=40)
        assert result.returncode == 0, result.stdout + result.stderr

    run("-m", "alembic", "upgrade", "0016")
    engine = create_engine(f"sqlite:///{database}")
    with engine.begin() as connection:
        connection.execute(insert(Group).values(id=-1001, approved=True, enabled=True))
        connection.execute(insert(Member).values(group_id=-1001, user_id=42, generation="current",
                                                  observed_messages=700))
        for identifier in range(1, 701):
            connection.execute(insert(Fingerprint).values(
                group_id=-1001, user_id=42, message_id=identifier, generation="current", sequence=identifier,
                digest=str(identifier), batch_key=f"m:{identifier}",
                created_at=time.time() - (7200 if identifier <= 100 else 100),
            ))
        connection.execute(insert(Fingerprint).values(
            group_id=-1001, user_id=42, message_id=900, generation="previous", sequence=1,
            digest="old", created_at=time.time() - 7200,
        ))
        for identifier in range(1, 111):
            connection.execute(insert(MemberText).values(
                group_id=-1001, user_id=42, message_id=identifier, digest=str(identifier),
            ))
        connection.execute(insert(ArchivedMessage).values(
            id="preserved-archive", group_id=-1001, message_id=999, sent_at=1, text="保留业务历史",
        ))
        for identifier, state in (("stranded-release", "releasing"), ("stranded-failure", "failing"),
                                  ("current-failure", "failing")):
            connection.execute(insert(Verification).values(
                id=identifier, group_id=-1001, user_id=42, generation="current", state=state,
                nonce="current-nonce", error="验证超时", deadline=time.time() - 1,
            ))
            kind = "release" if state == "releasing" else "verification_failure"
            prefix = "release" if state == "releasing" else "verification-failure"
            connection.execute(insert(Action).values(
                id=identifier + "-old", dedupe_key=f"{prefix}:{identifier}", kind=kind,
                group_id=-1001, user_id=42, state="cancelled" if kind == "release" else "succeeded",
                payload={"verification_id": identifier, "expected_nonce": "obsolete-nonce"},
            ))
        connection.execute(insert(Action).values(
            id="keep-failed-receipt", dedupe_key="current-failed", kind="verification_failure",
            group_id=-1001, user_id=42, state="failed", error="等待管理员处理",
            payload={"verification_id": "current-failure", "expected_nonce": "current-nonce"},
        ))
    engine.dispose()
    run("-m", "purrai.cli", "init-db")
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*),min(message_id),max(message_id) FROM member_texts").fetchone() == (100, 1, 100)
        assert connection.execute("SELECT count(*) FROM fingerprints").fetchone()[0] == 600
        assert connection.execute("SELECT sequence FROM fingerprints WHERE message_id=1").fetchone()[0] == 1
        assert connection.execute("SELECT text FROM archived_messages").fetchone()[0] == "保留业务历史"
        assert connection.execute("SELECT state,error FROM actions WHERE id='keep-failed-receipt'").fetchone() == (
            "failed", "等待管理员处理",
        )
        recovered = connection.execute("SELECT kind,payload FROM actions WHERE state='pending'").fetchall()
        assert len(recovered) == 2
        assert {json.loads(payload)["verification_id"] for _, payload in recovered} == {
            "stranded-release", "stranded-failure",
        }
        for query, index in (
            ("SELECT * FROM fingerprints WHERE group_id=-1001 AND user_id=42 AND generation='current' "
             "AND created_at>0 ORDER BY created_at DESC LIMIT 500", "ix_fingerprints_member_window"),
            ("SELECT * FROM fingerprints WHERE group_id=-1001 AND user_id=42 AND generation='current' "
             "AND batch_key='m:1'", "ix_fingerprints_batch"),
        ):
            plan = [row[-1] for row in connection.execute("EXPLAIN QUERY PLAN " + query)]
            assert any(index in row for row in plan)
            assert not any("TEMP B-TREE" in row for row in plan)


def test_action_batch_migration_preserves_old_receipts_and_indexes_children(tmp_path):
    root = Path(__file__).resolve().parents[1]
    database = tmp_path / "batches.sqlite3"
    environment = {**os.environ, "PURRAI_DATABASE_URL": f"sqlite+aiosqlite:///{database}",
                   "PURRAI_BACKUP_DIR": str(tmp_path / "backups")}

    def run(*arguments):
        result = subprocess.run([sys.executable, *arguments], cwd=root, env=environment,
                                capture_output=True, text=True, encoding="utf-8", timeout=40)
        assert result.returncode == 0, result.stdout + result.stderr

    run("-m", "alembic", "upgrade", "0015")
    with sqlite3.connect(database) as connection:
        for identifier, kind, state, payload in (
            ("old-batch", "batch_release", "succeeded", {"action_ids": ["old-child-1", "old-child-2"]}),
            ("old-child-1", "release", "pending", {"verification_id": "v1"}),
            ("old-child-2", "release", "succeeded", {"verification_id": "v2"}),
        ):
            connection.execute("INSERT INTO actions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                identifier, identifier, None, 42, kind, json.dumps(payload), state,
                0, 0, 1, "", 1, "保留已有批次", 1, 1,
            ))
    run("-m", "purrai.cli", "init-db")
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT id,batch_id,state FROM actions ORDER BY id").fetchall() == [
            ("old-batch", None, "succeeded"),
            ("old-child-1", "old-batch", "pending"),
            ("old-child-2", "old-batch", "succeeded"),
        ]
        plan = connection.execute("EXPLAIN QUERY PLAN SELECT state,count(*) FROM actions "
                                  "WHERE batch_id='old-batch' GROUP BY state").fetchall()
        assert any("ix_actions_batch_state" in row[-1] for row in plan)


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
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == schema_head()
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
        assert settings["advertising_ban"] and settings["external_group_ban"] and settings["external_invite_ban"]
        assert settings["telegram_allowed_links"] == []
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


def test_newcomer_migration_updates_defaults_and_preserves_custom_settings(tmp_path, monkeypatch):
    import importlib.util
    import sqlalchemy as sa

    script = Path(__file__).resolve().parents[1] / "migrations/versions/0018_newcomer_three_days.py"
    spec = importlib.util.spec_from_file_location("newcomer_period", script)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    original = [{}, {"newcomer_seconds": 600}, {"newcomer_seconds": 0},
                {"newcomer_seconds": 1200, "newcomer_forwards": False}]
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'newcomer.sqlite3'}")
    try:
        with engine.begin() as connection:
            connection.execute(sa.text("CREATE TABLE groups (id INTEGER PRIMARY KEY, settings TEXT NOT NULL)"))
            for identifier, settings in enumerate(original):
                connection.execute(sa.text("INSERT INTO groups VALUES (:id,:settings)"),
                                   {"id": identifier, "settings": json.dumps({**settings, "repeat_count": 9})})
            monkeypatch.setattr(migration.op, "get_bind", lambda: connection)
            migration.upgrade()
            migration.upgrade()
            rows = connection.execute(sa.text("SELECT settings FROM groups ORDER BY id")).scalars().all()
            for saved, before in zip(rows, original, strict=True):
                expected = {**before, "repeat_count": 9, "newcomer_wallets": True}
                expected.setdefault("newcomer_forwards", True)
                expected["newcomer_seconds"] = 259200 if before.get("newcomer_seconds", 600) == 600 else before["newcomer_seconds"]
                assert json.loads(saved) == expected
    finally:
        engine.dispose()


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
