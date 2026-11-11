"""Regression coverage for the six cohesion and duplication repairs."""

import ast
from pathlib import Path

import pytest
from sqlalchemy import select

from conftest import message
from purrai.errors import ManagementError
from purrai.management import Management
from purrai.models import Action, ArchivedMessage, Rule, Violation
from purrai.moderation_penalties import apply_penalty
from purrai.rule_evaluation import evaluate_message
from purrai.schemas import RuleInput, SharedPolicyPatch, WhitelistInput
from purrai.sender_identity import sender_name


def test_runtime_and_bot_boundaries_do_not_reintroduce_storage_details():
    root = Path(__file__).resolve().parents[1] / "src/purrai"
    for module in ("runtime", "bot_input", "bot_routes"):
        tree = ast.parse((root / f"{module}.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert node.module not in {"sqlalchemy", "purrai.serialization"}
                if node.module == "purrai.models":
                    assert {alias.name for alias in node.names} <= {"now", "uid"}
            if isinstance(node, ast.Attribute):
                assert node.attr not in {"__table__", "sessions", "transaction"}
    runner = ast.parse((root / "actions.py").read_text(encoding="utf-8"))
    methods = next(node for node in runner.body if isinstance(node, ast.ClassDef)).body
    # Dispatch may register domain names; runtime execution must not branch on
    # verification states, failure strategies or profile-specific generations.
    for method in methods:
        if isinstance(method, ast.AsyncFunctionDef):
            assert not {"nonce", "failure_action", "owned_restriction", "generation"} & {
                node.attr for node in ast.walk(method) if isinstance(node, ast.Attribute)
            }


async def test_partial_rule_edit_preserves_inheritance_fields_and_replays_before_merging(env):
    settings, db, tg, _ = env
    manager = Management(db, tg, settings=settings)
    shared = await manager.configuration.shared(1, SharedPolicyPatch(rules=[{
        "kind": "keyword", "value": "original", "mode": "exact", "scope": "name",
        "penalty": "permanent_mute",
    }], request_id="boundary-shared-rule"))
    identifier = shared["rules"][0]["id"]
    original = await manager.queries.detail(1, -1001, "rule", identifier)
    assert isinstance(original, dict) and original["inherited"] and original["version"]
    first = await manager.rules.patch_rule(
        1, -1001, identifier, {"enabled": False}, "boundary-rule-once", original["version"],
    )
    assert {key: first[key] for key in ("value", "mode", "scope", "penalty")} == {
        "value": "original", "mode": "exact", "scope": "name", "penalty": "permanent_mute",
    }
    assert not first["inherited"] and not first["enabled"]
    latest = await manager.rules.patch_rule(
        1, -1001, identifier, {"value": "newest"}, "boundary-rule-newest", first["version"],
    )
    replay = await manager.rules.patch_rule(
        1, -1001, identifier, {"enabled": False}, "boundary-rule-once", original["version"],
    )
    assert replay == first
    assert (await manager.queries.detail(1, -1001, "rule", identifier)) == latest
    with pytest.raises(ManagementError, match="记录已被修改"):
        await manager.rules.patch_rule(
            1, -1001, identifier, {"enabled": True}, "boundary-rule-stale", original["version"],
        )
    async with db.sessions() as session:
        rows = (await session.scalars(select(Rule).where(Rule.group_id == -1001))).all()
        assert len(rows) == 1 and rows[0].inherited_from == identifier


async def test_partial_whitelist_edit_keeps_other_flags_and_old_request_cannot_override_newer_edit(env):
    settings, db, tg, _ = env
    manager = Management(db, tg, settings=settings)
    original = await manager.whitelist(1, -1001, WhitelistInput(
        user_id=42, verification=False, moderation=True, note="original",
    ), expected_version="missing")
    first = await manager.rules.patch_whitelist(
        1, -1001, 42, {"note": "edited"}, "boundary-white-once", original["version"],
    )
    assert first["verification"] is False and first["moderation"] is True
    latest = await manager.rules.patch_whitelist(
        1, -1001, 42, {"moderation": False}, "boundary-white-newest", first["version"],
    )
    assert await manager.rules.patch_whitelist(
        1, -1001, 42, {"note": "edited"}, "boundary-white-once", original["version"],
    ) == first
    assert await manager.queries.detail(1, -1001, "whitelist", "42") == latest
    with pytest.raises(ManagementError, match="记录已被修改"):
        await manager.rules.patch_whitelist(
            1, -1001, 42, {"moderation": True}, "boundary-white-stale", original["version"],
        )


@pytest.mark.parametrize("sender, expected", [
    ({"first_name": "Alice", "last_name": "Smith", "username": "alice"}, "Alice Smith @alice"),
    ({"title": "频道", "username": "channel"}, "频道 @channel"),
    ({"username": "alice"}, "@alice"),
    ({"first_name": "Alice", "last_name": None}, "Alice"),
])
def test_sender_identity_is_consistent_for_users_channels_and_incomplete_profiles(sender, expected):
    assert sender_name(sender, include_username=True) == expected
    assert "@" not in sender_name(sender)
    assert len(sender_name({"first_name": "长" * 600}, include_username=True)) == 500


async def test_rule_assessment_evidence_and_archive_share_sender_identity(env):
    _, db, _, svc = env
    incoming = message(31, "普通正文")
    incoming["from"].update(first_name="Alice", last_name="Smith", username="blocked_user")
    async with db.transaction() as session:
        session.add(Rule(id="sender-rule", group_id=-1001, kind="keyword", value="@blocked_user", scope="name"))
    await svc.message(incoming)
    async with db.sessions() as session:
        violation = await session.scalar(select(Violation).where(Violation.message_id == 31))
        archive = await session.scalar(select(ArchivedMessage).where(ArchivedMessage.message_id == 31))
        task = await session.scalar(select(Action).where(Action.kind == "profile_mute"))
        assert violation.sender_name == "Alice Smith @blocked_user"
        assert task.payload["rule_evidence"]["sender_name"] == violation.sender_name
        assert archive.sender_name == "Alice Smith" and archive.username == "blocked_user"
        assessment = evaluate_message(incoming, {}, [RuleInput(kind="keyword", value="@blocked_user", scope="name").model_dump()])
        assert assessment.profile["name_reasons"] == violation.reasons


@pytest.mark.parametrize("profile, rule, expected", [
    (True, False, "profile_mute"), (False, True, "rule_mute"), (True, True, "profile_mute"),
])
async def test_shared_permanent_mute_enqueue_preserves_precedence_keys_and_evidence(env, profile, rule, expected):
    _, db, _, _ = env
    async with db.transaction() as session:
        from purrai.models import Group
        violation = Violation(id="mixed-evidence", group_id=-1001, user_id=42, message_id=32,
                              reasons=["广告规则命中"], sender_name="Alice @alice", excerpt="广告正文",
                              message_violation=True, profile_violation=profile, rule_mute=rule)
        session.add(violation)
        await session.flush()
        identifiers = await apply_penalty(session, await session.get(Group, -1001), violation, None)
        tasks = (await session.scalars(select(Action).where(Action.id.in_(identifiers)))).all()
        penalties = [task for task in tasks if task.kind != "delete"]
        assert len(penalties) == 1 and penalties[0].kind == expected
        assert penalties[0].dedupe_key == expected.replace("_", "-") + ":mixed-evidence"
        assert penalties[0].payload["generation"]
        assert penalties[0].payload["rule_evidence"]["sender_name"] == violation.sender_name
