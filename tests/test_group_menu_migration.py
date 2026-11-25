"""Menu migration adds storage without changing existing group or points policy."""
import os
from pathlib import Path
import sqlite3
import subprocess
import sys


def test_menu_upgrade_preserves_policy_and_saved_menu_on_rollback(tmp_path):
    database = tmp_path / "menu-migration.sqlite3"
    environment = {**os.environ, "PURRAI_DATABASE_URL": f"sqlite+aiosqlite:///{database}", "PYTHONUTF8": "1"}
    root = Path(__file__).resolve().parents[1]

    def migrate(command, revision):
        result = subprocess.run([sys.executable, "-m", "alembic", command, revision], cwd=root,
            env=environment, capture_output=True, text=True, timeout=40)
        assert result.returncode == 0, result.stdout + result.stderr

    migrate("upgrade", "0037")
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO groups (id,title,type,approved,enabled,status,permissions,settings,setting_overrides,activated_at,created_at) VALUES (-1001,'test','supergroup',1,1,'active','{}','{}','[]',0,1)")
        connection.execute("INSERT INTO points_policies VALUES (-1001,?,9)", ('{"enabled":false,"commands":{"signin":[]}}',))
        points = connection.execute("SELECT * FROM points_policies").fetchall()
        shared = connection.execute("SELECT * FROM shared_policy").fetchall()
    migrate("upgrade", "head")
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT * FROM group_menus").fetchall() == []
        connection.execute("INSERT INTO group_menus VALUES (-1001,?,4,NULL,'previous')", ('{"rows":[]}',))
        menu = connection.execute("SELECT * FROM group_menus").fetchall()
    migrate("downgrade", "0037")
    migrate("upgrade", "head")
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT * FROM group_menus").fetchall() == menu
        assert connection.execute("SELECT * FROM points_policies").fetchall() == points
        assert connection.execute("SELECT * FROM shared_policy").fetchall() == shared
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
