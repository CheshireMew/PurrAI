import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

from sqlalchemy import create_engine, insert

from purrai.models import Action, Group, Rule, default_settings


def test_shared_migration_keeps_custom_settings_local_rules_and_old_receipts(tmp_path):
    root = Path(__file__).resolve().parents[1]
    database = tmp_path / "shared.sqlite3"
    environment = {**os.environ, "PURRAI_DATABASE_URL": f"sqlite+aiosqlite:///{database}"}

    def upgrade(revision):
        result = subprocess.run([sys.executable, "-m", "alembic", "upgrade", revision], cwd=root,
                                env=environment, capture_output=True, text=True, timeout=40)
        assert result.returncode == 0, result.stdout + result.stderr

    upgrade("0021")
    engine = create_engine(f"sqlite:///{database}")
    custom = {**default_settings(), "chat_enabled": True, "chat_persona": "本群专属人设", "repeat_count": 9,
              "emoji_status_blocked_packs": ["local_pack"]}
    with engine.begin() as connection:
        connection.execute(insert(Group), [{"id": -1001, "settings": custom},
                                           {"id": -1002, "settings": default_settings()},
                                           {"id": -1003, "settings": default_settings()}])
        connection.execute(insert(Rule), [
            {"id": "a" * 32, "group_id": -1001, "kind": "keyword", "value": "通用广告"},
            {"id": "b" * 32, "group_id": -1002, "kind": "keyword", "value": "通用广告"},
            {"id": "c" * 32, "group_id": -1001, "kind": "keyword", "value": "本群广告"},
        ])
        for identifier, state in [("pending", "pending"), ("old", "succeeded")]:
            connection.execute(insert(Action).values(id=identifier, kind="emoji_status_ban", state=state,
                dedupe_key=identifier, group_id=-1001, user_id=42, payload={"message_ids": [77]}))
    engine.dispose()
    upgrade("head")
    with sqlite3.connect(database) as connection:
        settings, overrides = connection.execute("SELECT settings,setting_overrides FROM groups WHERE id=-1001").fetchone()
        settings, overrides = json.loads(settings), json.loads(overrides)
        assert settings == {**custom, "emoji_status_blocked_packs": ["local_pack", "tgvip_16"]}
        assert {"chat_enabled", "chat_persona", "repeat_count", "emoji_status_blocked_packs"} <= set(overrides)
        common = json.loads(connection.execute("SELECT rules FROM shared_policy WHERE id=1").fetchone()[0])
        assert [rule["value"] for rule in common] == ["通用广告"]
        assert connection.execute("SELECT count(*) FROM rules").fetchone()[0] == 3
        assert connection.execute("SELECT customized FROM rules WHERE id=?", ("c" * 32,)).fetchone()[0] == 1
        assert dict(connection.execute("SELECT id,kind FROM actions")) == {"pending": "emoji_status_mute", "old": "emoji_status_ban"}
