import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

from sqlalchemy import MetaData, create_engine, insert

from purrai.models import Action, Group, Meta, Rule


def test_review_upgrade_repairs_existing_corrections_and_preserves_points_receipts(tmp_path):
    from purrai.models import AdvertisementSample, PointsAccount, PointsEntry, Violation
    root = Path(__file__).resolve().parents[1]
    database = tmp_path / "review-consistency.sqlite3"
    env = {**os.environ, "PURRAI_DATABASE_URL": f"sqlite+aiosqlite:///{database}"}

    def upgrade(target):
        subprocess.run([sys.executable, "-m", "alembic", "upgrade", target], cwd=root, env=env, check=True)

    upgrade("0035")
    engine = create_engine(f"sqlite:///{database}")
    with engine.begin() as conn:
        conn.execute(insert(Group).values(id=-2002))
        conn.execute(insert(Violation).values(id="corrected", group_id=-2002, user_id=43, message_id=11))
        conn.execute(insert(AdvertisementSample).values(id="s", group_id=-2002, text="独立样本",
            source="model", source_key="review:s", violation_id="corrected", enabled=True, version=3))
        conn.execute(insert(Meta), [
            {"key": "violation-correction:corrected", "value": "{}"},
            {"key": "ai_advertising:-2002:43", "value": json.dumps({"messages": [{"id": 11}, {"id": 12}]})}])
        conn.execute(insert(PointsAccount).values(id="account", group_id=-2002, source_key="legacy", display_name="用户", balance=4))
        for key, kind, origin, intent in [
            ("chat:15", "chat", -1001, {}), ("admin-adjust:16", "adjustment", -2002, {"delta": 1}),
            ("admin-adjust:17", "adjustment", -2002, {"kind": "adjustment"})]:
            conn.execute(insert(PointsEntry).values(group_id=-2002, account_id="account", kind=kind, delta=1,
                balance_after=4, reason="测试", actor_id=1, request_id=key, result={"group_id": origin}, intent=intent))
    engine.dispose()
    upgrade("head")
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT enabled, version FROM advertisement_samples").fetchone() == (0, 4)
        assert json.loads(conn.execute("SELECT value FROM meta WHERE key='ai_advertising:-2002:43'").fetchone()[0]) == {
            "messages": [{"id": 12}]}
        assert {r[0] for r in conn.execute("SELECT request_id FROM points_entries")} == {
            "chat:-1001:15", "admin-adjust:-2002:16", "admin-adjust:17"}
        assert conn.execute("SELECT balance FROM points_accounts").fetchone() == (4,)
        assert "local_after" in {r[1] for r in conn.execute("PRAGMA table_info(advertisement_reviews)")}
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_policy_upgrade_preserves_edits_and_exposes_previous_implicit_actions(tmp_path):
    root = Path(__file__).resolve().parents[1]
    database = tmp_path / "policy.sqlite3"
    environment = {**os.environ, "PURRAI_DATABASE_URL": f"sqlite+aiosqlite:///{database}"}

    def migrate(direction, target):
        result = subprocess.run([sys.executable, "-m", "alembic", direction, target], cwd=root,
                                env=environment, capture_output=True, text=True, timeout=40)
        assert result.returncode == 0, result.stdout + result.stderr

    migrate("upgrade", "0033")
    legacy = Rule.__table__.to_metadata(MetaData())
    legacy._columns.remove(legacy.c.name_penalty)
    engine = create_engine(f"sqlite:///{database}")
    custom = {"chat_ignored_texts": [], "repeat_count": 9, "notice_seconds": 45}
    with engine.begin() as connection:
        connection.execute(insert(Group).values(id=-1001, settings=custom, setting_overrides=["repeat_count", "notice_seconds"]))
        connection.execute(insert(legacy).values(id="local-target", group_id=-1001, kind="block_target", value="mention:arbitrary", penalty="delete"))
        connection.execute(insert(Meta).values(key="chat_input:-1001", value=json.dumps({"commands": {}, "ads": {}, "advertisers": {"42": 2800}})))
        connection.execute(insert(Action).values(id="notice", kind="warn", dedupe_key="notice", state="succeeded", group_id=-1001,
            updated_at=1000, payload={"notice_expires_at": 1180}))
        connection.execute(insert(Action).values(id="cleanup", kind="delete", dedupe_key="cleanup:notice:notice", state="pending", group_id=-1001,
            available_at=1180, payload={"message_id": 7}))
    engine.dispose()
    with sqlite3.connect(database) as connection:
        settings, rules, document = connection.execute("select settings,rules,business_rules from shared_policy where id=1").fetchone()
        document = json.loads(document)
        document.pop("ai_review_instructions", None)
        document["recognition"]["phone_prefixes"] = []
        document["stages"][0]["rules"] = []
        for stage in document["stages"]:
            for rule in stage["rules"]:
                rule.pop("advertiser_cooldown", None)
        document["stages"][1]["enabled"] = False
        connection.execute("update shared_policy set rules=?,business_rules=? where id=1", (json.dumps([
            {"id": "shared-target", "kind": "block_target", "value": "mention:arbitrary", "penalty": "delete", "enabled": False}]), json.dumps(document)))
    migrate("upgrade", "head")
    with sqlite3.connect(database) as connection:
        saved_settings, saved_rules, saved_doc = map(json.loads, connection.execute("select settings,rules,business_rules from shared_policy where id=1").fetchone())
        assert all(saved_settings[key] == value for key, value in json.loads(settings).items())
        assert saved_settings["notice_seconds"] == 30
        assert saved_rules[0]["penalty"] == "ban" and saved_rules[0]["enabled"] is False
        assert saved_rules[0]["name_penalty"] == "permanent_mute"
        assert saved_doc["recognition"]["phone_prefixes"] == []
        assert saved_doc["stages"][0]["rules"] == [] and not saved_doc["stages"][1]["enabled"]
        assert saved_doc["ai_review_instructions"]
        for stage in saved_doc["stages"]:
            for rule in stage["rules"]:
                assert type(rule["advertiser_cooldown"]) is bool
        assert connection.execute("select penalty,name_penalty from rules where id='local-target'").fetchone() == ("ban", "permanent_mute")
        group = json.loads(connection.execute("select settings from groups where id=-1001").fetchone()[0])
        assert all(group[key] == value for key, value in custom.items())
        assert connection.execute("select available_at from actions where id='cleanup'").fetchone()[0] == 1045
        assert json.loads(connection.execute("select value from meta where key='chat_input:-1001'").fetchone()[0])["advertisers"] == {"42": {"at": 1000}}
        saved_doc["ai_review_instructions"] = "管理员修改后的审核定义"
        saved_rules[0]["penalty"] = "delete"
        connection.execute("update shared_policy set rules=?,business_rules=? where id=1", (json.dumps(saved_rules), json.dumps(saved_doc)))
        connection.execute("update rules set penalty='delete',name_penalty='ban' where id='local-target'")
    migrate("downgrade", "0033")
    migrate("upgrade", "head")
    with sqlite3.connect(database) as connection:
        assert list(map(json.loads, connection.execute("select rules,business_rules from shared_policy where id=1").fetchone())) == [saved_rules, saved_doc]
        assert connection.execute("select penalty,name_penalty from rules where id='local-target'").fetchone() == ("delete", "ban")
        assert connection.execute("pragma integrity_check").fetchone()[0] == "ok"


def test_reason_and_timing_migration_preserves_saved_empty_rules_and_new_dispositions(tmp_path):
    root = Path(__file__).resolve().parents[1]
    database = tmp_path / 'reason-policy.sqlite3'
    environment = {**os.environ, 'PURRAI_DATABASE_URL': f'sqlite+aiosqlite:///{database}'}
    def migrate(direction, target):
        result = subprocess.run([sys.executable, '-m', 'alembic', direction, target], cwd=root,
                                env=environment, capture_output=True, text=True, timeout=40)
        assert result.returncode == 0, result.stdout + result.stderr
    migrate('upgrade', '0034')
    with sqlite3.connect(database) as c:
        before = list(map(json.loads, c.execute('select settings,rules,business_rules from shared_policy where id=1').fetchone()))
    engine = create_engine(f'sqlite:///{database}')
    with engine.begin() as connection:
        connection.execute(insert(Group).values(id=-1001, settings={'chat_context_hours': 9}, setting_overrides=['chat_context_hours']))
    engine.dispose()
    migrate('upgrade', 'head')
    with sqlite3.connect(database) as c:
        settings, rules, document = map(json.loads, c.execute('select settings,rules,business_rules from shared_policy where id=1').fetchone())
        assert rules == before[1]
        assert {k:v for k,v in document.items() if k != 'reason_policy'} == before[2]
        assert document['reason_policy']['ban']
        assert settings['chat_message_max_age_seconds'] == 120
        assert json.loads(c.execute('select settings from groups where id=-1001').fetchone()[0])['chat_context_hours'] == 9
        document['reason_policy']['ban'] = []
        document['reason_policy']['ai_history'][0]['enabled'] = False
        c.execute('update shared_policy set business_rules=?', (json.dumps(document),))
        assert 'reason_details' in {row[1] for row in c.execute('pragma table_info(violations)')}
    migrate('downgrade', '0034')
    migrate('upgrade', 'head')
    with sqlite3.connect(database) as c:
        assert json.loads(c.execute('select business_rules from shared_policy').fetchone()[0]) == document
        assert c.execute('pragma integrity_check').fetchone()[0] == 'ok'
