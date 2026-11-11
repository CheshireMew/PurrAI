import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from purrai.db import schema_head


def test_temporary_state_upgrade_preserves_history_and_grants_existing_receipts_a_full_window(tmp_path):
    database = tmp_path / "temporary.sqlite3"
    environment = {**os.environ, "PURRAI_DATABASE_URL": f"sqlite+aiosqlite:///{database}",
                   "PURRAI_BACKUP_DIR": str(tmp_path / "backups")}
    root = Path(__file__).resolve().parents[1]
    def run(target):
        result = subprocess.run([sys.executable, "-m", "alembic", "upgrade", target], cwd=root,
                                env=environment, capture_output=True, text=True, timeout=40)
        assert result.returncode == 0, result.stdout + result.stderr
    run("0022")
    with sqlite3.connect(database) as connection:
        connection.executemany("INSERT INTO meta (key,value) VALUES (?,?)", [
            ("bot_button:old", "{}"), ("callback:old", "answer"), ("permanent", "keep")])
        connection.executemany("INSERT INTO inbox (id,payload,state,attempts,available_at,error) VALUES (?,?,?,0,0,'')",
                               [(1, "{}", "done"), (2, "{}", "failed"), (3, "{}", "pending")])
    before = time.time()
    run("head")
    with sqlite3.connect(database) as connection:
        rows = dict(connection.execute("SELECT key,expires_at FROM meta"))
        assert rows["bot_button:old"] >= before + 30 * 86400
        assert rows["callback:old"] >= before + 7 * 86400
        assert rows["permanent"] == 0
        records = dict(connection.execute("SELECT id,completed_at FROM inbox"))
        assert records[1] >= before and records[2] == records[3] == 0
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone()[0] == schema_head()
