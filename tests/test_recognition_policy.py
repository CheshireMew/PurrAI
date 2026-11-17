import copy
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from conftest import message
from purrai import chat_commands
from purrai.chat_state import ignored_text
from purrai.models import now
from purrai.recognition_policy import RecognitionPolicy, default_recognition
from purrai.schemas import GroupSettings
from purrai.shared_policy import effective_rules


async def save(client, document):
    current = (await client.get("/api/settings/business-policy")).json()
    response = await client.put("/api/settings/business-policy", headers={"If-Match": current["version"]},
        json={"document": document, "request_id": f"recognition-{now()}"})
    assert response.status_code == 200, response.text
    return response.json()


async def preview(client, document, text):
    response = await client.post("/api/settings/business-policy/preview",
                                 json={"group_id": -1001, "document": document, "text": text})
    assert response.status_code == 200, response.text
    return response.json()["after"]


@pytest.mark.parametrize(("section", "first", "second", "sample"), [
    ("utility_patterns", "琥珀口令[0-9]+", "珊瑚口令[0-9]+", "{}40"),
    ("candidate_patterns", "琥珀口令", "珊瑚口令", "{}40"),
    ("other_bot_patterns", "amber", "coral", "@other_bot {}"),
    ("output_patterns", "琥珀口令", "珊瑚口令", "{}"),
    ("phone_prefixes", "琥珀口令", "珊瑚口令", "{}13800138000"),
    ("shortener_domains", "amber.example", "coral.example", "https://{}/offer"),
])
async def test_each_recognition_category_saves_replaces_disables_and_clears(client, env, section, first, second, sample):
    current = (await client.get("/api/settings/business-policy")).json()
    document = current["document"]
    original = copy.deepcopy({key: value for key, value in document.items() if key != "recognition"})
    document["recognition"] = RecognitionPolicy().model_dump()
    key = "pattern" if section.endswith("patterns") else "value"
    entry = {"id": "arbitrary-example", "title": "任意识别样例", "enabled": True, key: first}
    document["recognition"][section] = [entry]

    def text(value):
        return sample.format(value.removesuffix("[0-9]+"))

    def hit(result):
        if section in ("utility_patterns", "other_bot_patterns"):
            return result["utility_command"]
        if section == "candidate_patterns":
            return bool(result["command_candidate"])
        if section == "output_patterns":
            return "任意识别样例" in result["output_reasons"]
        prefix = "phone:" if section == "phone_prefixes" else "url:"
        return any(target.startswith(prefix) for target in result["advertised_targets"])

    saved = await save(client, document)
    assert hit(await preview(client, saved["document"], text(first)))
    entry[key] = second
    saved = await save(client, document)
    assert not hit(await preview(client, saved["document"], text(first)))
    assert hit(await preview(client, saved["document"], text(second)))
    entry["enabled"] = False
    saved = await save(client, document)
    assert not hit(await preview(client, saved["document"], text(second)))
    document["recognition"][section] = []
    await save(client, document)
    reloaded = (await client.get("/api/settings/business-policy")).json()["document"]
    assert reloaded["recognition"] == RecognitionPolicy().model_dump()
    assert {key: value for key, value in reloaded.items() if key != "recognition"} == original
    async with env[1].sessions() as session:
        rules = await effective_rules(session, -1001)
        assert not ignored_text("积分排行", [], rules=rules)


async def test_disabling_candidate_rule_invalidates_already_learned_commands(client, env):
    document = (await client.get("/api/settings/business-policy")).json()["document"]
    document["recognition"]["candidate_patterns"] = [
        {"id": "custom-learning", "title": "测试学习", "enabled": True, "pattern": "琥珀口令"}]
    await save(client, document)
    cfg = GroupSettings()
    async with env[1].sessions() as session:
        rules = await effective_rules(session, -1001)
    state = {"commands": {"琥珀口令": [[i, 42+i%3, now()] for i in range(cfg.chat_command_threshold)]}, "ads": {}}
    assert chat_commands.learned(state, cfg, rules) == ["琥珀口令"]
    document["recognition"]["candidate_patterns"][0]["enabled"] = False
    await save(client, document)
    async with env[1].sessions() as session:
        rules = await effective_rules(session, -1001)
    assert chat_commands.learned(state, cfg, rules) == []
    assert not ignored_text("琥珀口令40", chat_commands.learned(state, cfg, rules), rules=rules)


async def test_old_client_cannot_silently_remove_new_policy_sections(client):
    current = (await client.get("/api/settings/business-policy")).json()
    current["document"].pop("recognition")
    response = await client.put("/api/settings/business-policy", headers={"If-Match": current["version"]},
        json={"document": current["document"], "request_id": "old-client-policy-save"})
    assert response.status_code == 422
    assert (await client.get("/api/settings/business-policy")).json()["version"] == current["version"]


async def test_preview_uses_real_bot_identity_for_other_bot_commands(client, env):
    env[2].username = "this_bot"
    document = (await client.get("/api/settings/business-policy")).json()["document"]
    assert not (await preview(client, document, "@this_bot balance"))["utility_command"]
    assert (await preview(client, document, "@another_bot balance"))["utility_command"]


async def test_saved_recognition_controls_actual_chat_and_independent_review(client, env):
    from purrai.models import AdvertisementReview
    from sqlalchemy import select
    from test_chat import configured

    chat, model = await configured(env)
    document = (await client.get("/api/settings/business-policy")).json()["document"]
    document["recognition"]["utility_patterns"] = [
        {"id": "custom-utility", "title": "测试口令", "enabled": True, "pattern": "琥珀口令[0-9]+"}]
    await save(client, document)
    await chat.observe(message(601, "琥珀口令40"))
    assert not await chat.run_one() and not model.calls
    reviewer = env[3].advertisement_review
    await reviewer.observe(message(601, "琥珀口令40"))
    async with env[1].sessions() as session:
        row = await session.scalar(select(AdvertisementReview).where(AdvertisementReview.message_id == 601))
        assert row.state == "ignored"
    document["recognition"]["utility_patterns"][0]["enabled"] = False
    await save(client, document)
    await reviewer.observe(message(602, "琥珀口令40"))
    async with env[1].sessions() as session:
        row = await session.scalar(select(AdvertisementReview).where(AdvertisementReview.message_id == 602))
        assert row.state == "pending"
    await chat.close()


def test_recognition_migration_preserves_existing_configuration_and_empty_lists(tmp_path):
    root = Path(__file__).resolve().parents[1]
    database = tmp_path / "recognition.sqlite3"
    environment = {**os.environ, "PURRAI_DATABASE_URL": f"sqlite+aiosqlite:///{database}"}

    def migrate(direction, target):
        result = subprocess.run([sys.executable, "-m", "alembic", direction, target], cwd=root,
                                env=environment, capture_output=True, text=True, timeout=40)
        assert result.returncode == 0, result.stdout + result.stderr

    migrate("upgrade", "0032")
    with sqlite3.connect(database) as connection:
        before = connection.execute("select settings,rules,business_rules from shared_policy where id=1").fetchone()
        document = json.loads(before[2])
        document.pop("recognition", None)  # Reproduce an actual pre-0033 saved document.
        document["stages"][0]["rules"] = []
        connection.execute("update shared_policy set business_rules=? where id=1", (json.dumps(document),))
    migrate("upgrade", "0033")
    with sqlite3.connect(database) as connection:
        settings, rules, value = connection.execute("select settings,rules,business_rules from shared_policy where id=1").fetchone()
        assert (settings, rules) == before[:2]
        assert json.loads(value) == {**document, "recognition": default_recognition()}
        document["recognition"] = RecognitionPolicy().model_dump()
        connection.execute("update shared_policy set business_rules=? where id=1", (json.dumps(document),))
    migrate("downgrade", "0032")
    migrate("upgrade", "0033")
    with sqlite3.connect(database) as connection:
        assert json.loads(connection.execute("select business_rules from shared_policy where id=1").fetchone()[0]) == document
        assert connection.execute("pragma integrity_check").fetchone()[0] == "ok"


def test_initial_list_data_does_not_replace_saved_empty_settings():
    from purrai.installation_defaults import initial_list

    for key in ("chat_ignored_texts", "emoji_status_blocked_packs", "chat_sticker_packs", "chat_antifraud_packs"):
        assert getattr(GroupSettings(), key) == initial_list(key)
        assert getattr(GroupSettings.model_validate({key: []}), key) == []
