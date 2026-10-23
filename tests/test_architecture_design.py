"""Protect the dependency and shared-contract boundaries established by the refactor."""

import ast
import json
import subprocess
import sys
from pathlib import Path

from sqlalchemy import select

from conftest import drain
from purrai.admin_catalog import admin_catalog
from purrai.management import Management
from purrai.models import Action, Group, Violation
from purrai.moderation_records import record_violation
from purrai.rules import CONTACT_REASON
from purrai.schemas import GroupSettings, SettingsPatch
from test_contact_kick import card
from test_verification import fixed_challenge

ROOT = Path(__file__).resolve().parents[1]


def module_dependencies():
    trees = {
        path.stem: ast.parse(path.read_text(encoding="utf-8")) for path in (ROOT / "src/purrai").glob("*.py")
    }
    graph = {name: set() for name in trees}
    for name, tree in trees.items():
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if (node.module or "").startswith("purrai."):
                    graph[name].add(node.module.split(".")[1])
                elif node.module == "purrai":
                    graph[name].update(alias.name for alias in node.names if alias.name in trees)
    return graph


def test_production_dependencies_are_acyclic_including_function_local_imports():
    graph, visited, path = module_dependencies(), set(), []

    def visit(name):
        assert name not in path, " -> ".join(path + [name])
        if name in visited:
            return
        path.append(name)
        for dependency in graph[name]:
            visit(dependency)
        path.pop()
        visited.add(name)

    for name in graph:
        visit(name)


def test_domain_modules_do_not_depend_on_application_facades():
    graph = module_dependencies()
    allowed = {"service": {"app"}, "management": {"runtime", "cli"}, "bot_admin": {"runtime"}}
    for facade, callers in allowed.items():
        assert {name for name, imports in graph.items() if facade in imports} <= callers
    assert graph["schemas"] == {"pack_names"}
    assert graph["pack_names"] == set()
    assert graph["serialization"] == set()


async def test_catalog_and_browser_fixture_match_the_authoritative_schema(client):
    response = await client.get("/api/catalog")
    expected = admin_catalog()
    assert response.status_code == 200 and response.json() == expected
    assert expected["settings"]["defaults"] == GroupSettings().model_dump()
    fixture = ROOT / "frontend/e2e/fixtures/catalog.json"
    assert json.loads(fixture.read_text(encoding="utf-8")) == expected
    types = (ROOT / "frontend/src/groupSettings.ts").read_text(encoding="utf-8")
    definition = types.split("export interface GroupSettings {", 1)[1].split("}", 1)[0]
    assert {line.strip().split(":")[0] for line in definition.splitlines() if ":" in line} == set(
        GroupSettings.model_fields
    )
    subprocess.run([sys.executable, str(ROOT / "scripts/sync_admin_contract.py"), "--check"], check=True)


async def test_http_and_telegram_record_queries_share_projection_and_filtering(env, client, monkeypatch):
    verification = await fixed_challenge(env, monkeypatch)
    settings, db, tg, service = env
    management = Management(db, tg, service, settings)
    records, total = await management.records(1, -1001, "verification", pending=True)
    response = await client.get("/api/groups/-1001/verifications?state=pending&size=5")
    assert response.json()["items"] == records
    assert total == response.json()["total"] == 1
    assert records[0]["id"] == verification.id
    assert not {"answer_hash", "input_digits", "nonce"} & records[0].keys()
    system = (await client.get("/api/system")).json()
    shared = await management.queries.system(1)
    assert {key: system[key] for key in shared} == shared


async def test_single_and_batch_settings_updates_share_validation_conflict_and_replay(env):
    settings, db, tg, service = env
    management = Management(db, tg, service, settings)
    changed = await management.setting(1, -1001, "repeat_count", 7, "shared-setting")
    replayed = await management.settings_patch(
        1, -1001, SettingsPatch(settings={"repeat_count": 8}), "shared-setting"
    )
    assert changed == replayed and replayed["settings"]["repeat_count"] == 7
    await management.settings_patch(1, -1001, SettingsPatch(settings={"similar_count": 6}))
    async with db.sessions() as session:
        group = await session.get(Group, -1001)
        assert (group.settings["repeat_count"], group.settings["similar_count"]) == (7, 6)


async def test_existing_message_evidence_can_upgrade_to_contact_ban(env):
    _, db, tg, service = env
    async with db.transaction() as session:
        row, changed = await record_violation(
            session,
            group_id=-1001,
            user_id=42,
            message_id=10,
            reasons=["关键词：旧广告"],
            message_violation=True,
        )
        identifier = row.id
        assert changed
    await service.message(card())
    await drain(service)
    async with db.sessions() as session:
        row = await session.get(Violation, identifier)
        assert row.reasons == ["关键词：旧广告", CONTACT_REASON]
        kick = await session.scalar(select(Action).where(Action.kind == "contact_ban"))
        assert kick.state == "succeeded" and kick.payload["violation_id"] == identifier
    assert tg.members[(-1001, 42)]["status"] == "kicked"


async def test_repeated_evidence_does_not_repeat_penalty_but_new_flags_merge(env):
    _, db, _, _ = env
    async with db.transaction() as session:
        base = dict(group_id=-1001, user_id=42, message_id=88, message_violation=True)
        original, _ = await record_violation(session, **base, reasons=["正文"])
        repeated, changed = await record_violation(session, **base, reasons=["正文"])
        assert repeated.id == original.id and not changed
        upgraded, changed = await record_violation(session, **base, reasons=["昵称"], profile_violation=True)
        assert changed and upgraded.profile_violation and upgraded.message_violation
        assert upgraded.reasons == ["正文", "昵称"]
