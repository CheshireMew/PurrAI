"""Upgrade existing ledgers without reseeding policy or changing balances."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

from sqlalchemy import create_engine, inspect

from purrai.models import Lottery, LotteryEntry


def test_lottery_upgrade_keeps_existing_points_and_policy(tmp_path):
    database = tmp_path / "migration.sqlite3"
    root = Path(__file__).resolve().parents[1]
    environment = {**os.environ, "PURRAI_DATABASE_URL": f"sqlite+aiosqlite:///{database}", "PYTHONUTF8": "1"}

    def upgrade(target):
        result = subprocess.run([sys.executable, "-m", "alembic", "upgrade", target], cwd=root,
            env=environment, capture_output=True, text=True, timeout=40)
        assert result.returncode == 0, result.stdout + result.stderr

    upgrade("0036")
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO groups (id,title,type,approved,enabled,status,permissions,settings,setting_overrides,activated_at,created_at) VALUES (-1001,'test','supergroup',1,1,'active','{}','{}','[]',0,1)")
        connection.execute("INSERT INTO points_accounts (id,group_id,source_key,display_name,user_id,balance,note,version,created_at) VALUES ('account',-1001,'original','群友',42,888,'保留',3,1)")
        connection.execute("INSERT INTO points_policies (group_id,config,version) VALUES (-1001,?,5)",
                           (json.dumps({"enabled": False, "commands": {"signin": []}}),))
        connection.execute("INSERT INTO whitelist (group_id,user_id,verification,moderation,note) VALUES (-1001,42,0,1,'手动保留')")
        white_before = connection.execute("SELECT group_id,user_id,verification,moderation,note FROM whitelist").fetchall()
        before = connection.execute("SELECT * FROM points_accounts").fetchall()
        policy = connection.execute("SELECT * FROM points_policies").fetchall()
        shared = connection.execute("SELECT * FROM shared_policy").fetchall()
    upgrade("head")
    upgrade("head")
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO lotteries VALUES ('draw',-1001,'{}','drawn',2,1,1,10,1,1,1,2)")
        connection.execute("INSERT INTO lottery_entries VALUES ('receipt','draw',-1001,42,'群友','request','2026-11-23',10,'won',0,'奖品',50,1,1)")
        lottery_before = connection.execute("SELECT * FROM lotteries").fetchall()
        entries_before = connection.execute("SELECT * FROM lottery_entries").fetchall()
    result = subprocess.run([sys.executable, "-m", "alembic", "downgrade", "0036"], cwd=root,
                            env=environment, capture_output=True, text=True, timeout=40)
    assert result.returncode == 0, result.stdout + result.stderr
    upgrade("head")
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT * FROM points_accounts").fetchall() == before
        assert connection.execute("SELECT * FROM points_policies").fetchall() == policy
        assert connection.execute("SELECT * FROM shared_policy").fetchall() == shared
        assert connection.execute("SELECT group_id,user_id,verification,moderation,note FROM whitelist").fetchall() == white_before
        assert connection.execute("SELECT administrator_backup FROM whitelist").fetchone()[0] is None
        assert connection.execute("SELECT * FROM lotteries").fetchall() == lottery_before
        assert connection.execute("SELECT * FROM lottery_entries").fetchall() == entries_before
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    engine = create_engine(f"sqlite:///{database}")
    try:
        inspector = inspect(engine)
        for model in (Lottery, LotteryEntry):
            assert {column["name"] for column in inspector.get_columns(model.__tablename__)} == set(model.__table__.columns.keys())
            assert {index["name"] for index in inspector.get_indexes(model.__tablename__)} == {index.name for index in model.__table__.indexes}
    finally:
        engine.dispose()
