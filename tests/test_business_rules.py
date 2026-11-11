"""Legacy equivalence plus persistence and arbitrary editable policy behavior."""

import copy
import json
from pathlib import Path

import pytest

from purrai import advertising_patterns as advertising, chat_input, solicitation_rules as solicitation
from purrai.business_rules import BusinessEngine, RuleSet, default_business_rules
from purrai.business_validation import validate_document
from purrai.text_matching import normalize
from purrai.shared_policy import effective_rules
from purrai.rules import assess
from conftest import drain, message


def test_installation_policy_is_valid_and_independently_copied():
    document = validate_document(default_business_rules())
    document["stages"][0]["enabled"] = False
    assert default_business_rules()["stages"][0]["enabled"]


def test_all_recorded_legacy_business_behaviors_are_preserved():
    fixture = Path(__file__).parent / "fixtures/legacy_business_behaviors.json"
    for case in json.loads(fixture.read_text(encoding="utf-8")):
        text = case["text"]
        canonical = normalize(text)
        actual = {
            "solicitation": solicitation.solicitation_reasons(text),
            "short": solicitation.short_solicitation_reasons(text),
            "profile": solicitation.profile_solicitation_reasons(text),
            "advertising": advertising.advertising_reasons(canonical),
            "advertising_source": advertising.advertising_reasons(canonical, ["免费跟单实时策略"]),
            "communication": solicitation.communication_context(text),
            "discussion": advertising.discussion(canonical),
            "chat_solicitation": chat_input.solicitation(text),
            "chat": chat_input.advertising(text),
            "chat_direct": chat_input.advertising(text, direct=True),
        }
        assert actual == case["expected"], (text, actual, case["expected"])


def test_policy_snapshot_changes_are_not_equal_and_survive_copying():
    original = RuleSet([], default_business_rules())
    changed = copy.deepcopy(original)
    changed.business["stages"][0]["enabled"] = False
    assert original != changed
    assert original == []


@pytest.mark.parametrize("section", ["views", "conditions", "stages"])
def test_circular_references_are_rejected(section):
    document = default_business_rules()
    item = document[section][0]
    if section == "views":
        item["source"] = item["id"]
    elif section == "conditions":
        item["condition"] = {"op": "ref", "ref": item["id"]}
    else:
        item["before"] = [item["id"]]
    with pytest.raises(ValueError, match="循环引用"):
        validate_document(document)


async def save_policy(client, document, version, request_id):
    response = await client.put("/api/settings/business-policy", headers={"If-Match": version},
        json={"document": document, "request_id": request_id})
    assert response.status_code == 200, response.text
    return response.json()


def arbitrary_rule(stage, word="星云橙灯", action=None):
    return {"id": "arbitrary-editable-rule", "title": "任意替换词验证", "enabled": True,
            "reason": "任意规则命中", "action": action or ("exclude" if stage.startswith("chat_") else "delete"),
            "condition": {"op": "match", "input": "s_value", "pattern": word, "mode": "search"}}


@pytest.mark.parametrize("stage", ["solicitation", "short", "profile", "advertising", "chat_solicitation", "chat_advertising"])
async def test_every_rule_family_can_be_created_edited_disabled_and_removed_through_admin(env, client, stage):
    _, db, _, _ = env
    initial = (await client.get("/api/settings/business-policy")).json()
    document = copy.deepcopy(initial["document"])
    group = next(item for item in document["stages"] if item["id"] == stage)
    group["rules"].insert(0, arbitrary_rule(stage))
    preview = await client.post("/api/settings/business-policy/preview", json={"document": document,
        "group_id": -1001, "text": "星云橙灯", "sender_name": "星云橙灯"})
    assert preview.status_code == 200, preview.text
    assert preview.json()["changed"]
    # Draft preview must not persist anything.
    assert (await client.get("/api/settings/business-policy")).json() == initial
    saved = await save_policy(client, document, initial["version"], "business-create-" + stage)

    async def matches_stage(text):
        async with db.sessions() as session:
            rules = await effective_rules(session, -1001)
        return any(hit.id == "arbitrary-editable-rule" for hit in BusinessEngine(rules).run(stage, text))

    assert await matches_stage("星云橙灯")
    group["rules"][0]["condition"]["pattern"] = "琥珀雪桥"
    saved = await save_policy(client, document, saved["version"], "business-edit-" + stage)
    assert not await matches_stage("星云橙灯")
    assert await matches_stage("琥珀雪桥")
    group["rules"][0]["enabled"] = False
    saved = await save_policy(client, document, saved["version"], "business-disable-" + stage)
    assert not await matches_stage("琥珀雪桥")
    group["rules"].pop(0)
    saved = await save_policy(client, document, saved["version"], "business-remove-" + stage)
    assert not await matches_stage("琥珀雪桥")
    assert saved["document"] == initial["document"]


@pytest.mark.parametrize("action", ["delete", "mute", "ban"])
@pytest.mark.parametrize("stage_id", ["advertising", "short", "profile"])
async def test_configured_disposition_reaches_real_moderation_and_keeps_its_meaning_after_json(env, client, action, stage_id):
    _, db, telegram, service = env
    saved = (await client.get("/api/settings/business-policy")).json()
    document = saved["document"]
    stage = next(item for item in document["stages"] if item["id"] == stage_id)
    item = arbitrary_rule(stage_id, action=action)
    # An old ban-category description must not override the edited disposition.
    item["reason"] = "明确广告引流：任意规则命中"
    stage["rules"].insert(0, item)
    await save_policy(client, document, saved["version"], "business-action-" + action)
    incoming = message(770, "正常发言" if stage_id == "profile" else "星云橙灯")
    if stage_id == "profile":
        incoming["from"]["first_name"] = "星云橙灯"
    await service.message(incoming)
    await drain(service)
    calls = [method for method, _ in telegram.calls]
    assert "delete_message" in calls
    assert ("ban_chat_member" in calls) == (action == "ban")
    assert ("restrict_chat_member" in calls) == (action == "mute")
    from sqlalchemy import select
    from purrai.models import Violation
    async with db.sessions() as session:
        violation = await session.scalar(select(Violation))
        assert any(advertising.is_ban_reason(reason) for reason in violation.reasons) == (action == "ban")


async def test_queued_ban_and_precomputed_assessment_recheck_changed_business_policy(env, client):
    _, db, telegram, service = env
    from purrai.rule_evaluation import prepare_message
    saved = (await client.get("/api/settings/business-policy")).json()
    document = saved["document"]
    stage = next(item for item in document["stages"] if item["id"] == "advertising")
    stage["rules"].insert(0, arbitrary_rule("advertising", action="ban"))
    saved = await save_policy(client, document, saved["version"], "business-queue-create")
    incoming = message(771, "星云橙灯")
    assessment = await prepare_message(db, incoming)
    assert assessment.profile["ban_reasons"]
    await service.message(incoming)
    stage["rules"][0]["enabled"] = False
    await save_policy(client, document, saved["version"], "business-queue-disable")
    from purrai.models import Group
    async with db.sessions() as session:
        assert not await assessment.current(session, await session.get(Group, -1001))
    await drain(service)
    assert not any(method == "ban_chat_member" for method, _ in telegram.calls)


async def test_normalization_exemptions_contact_and_chat_use_the_saved_snapshot(env, client):
    _, db, _, _ = env
    saved = (await client.get("/api/settings/business-policy")).json()
    document = saved["document"]
    stage = next(item for item in document["stages"] if item["id"] == "solicitation")
    stage["rules"].insert(0, arbitrary_rule("solicitation", action="ban"))
    view = next(item for item in document["views"] if item["id"] == "s_norm")
    view["steps"][1]["value"]["曇"] = "灯"
    shared = next(item for item in document["conditions"] if item["id"] == "communication_context")
    shared["condition"] = {"op": "any", "items": [shared["condition"],
        {"op": "match", "input": "s_value", "pattern": "琥珀雪桥", "mode": "search"}]}
    saved = await save_policy(client, document, saved["version"], "business-normalize-save")
    async with db.sessions() as session:
        rules = await effective_rules(session, -1001)
    assert assess("星云橙曇", [], rules, {})["ban_reasons"]
    assert not assess("琥珀雪桥星云橙曇", [], rules, {})["ban_reasons"]
    assert solicitation.contact_should_block({"contact": {"first_name": "普通成员"}, "text": "星云橙曇"}, {"contact_ads_only": True}, rules)
    stage["rules"][0]["enabled"] = False
    await save_policy(client, document, saved["version"], "business-normalize-disable")
    async with db.sessions() as session:
        rules = await effective_rules(session, -1001)
    assert not solicitation.contact_should_block({"contact": {"first_name": "普通成员"}, "text": "星云橙曇"}, {"contact_ads_only": True}, rules)
    # Removing all migrated rules must not resurrect any installation seed.
    for item in document["stages"]:
        item["rules"] = []
    latest = (await client.get("/api/settings/business-policy")).json()
    await save_policy(client, document, latest["version"], "business-empty-save")
    async with db.sessions() as session:
        rules = await effective_rules(session, -1001)
    from purrai.chat_safety import output_reasons
    assert not chat_input.input_reasons("来回血", rules, {})
    assert not output_reasons("免费带单私聊我", rules, {})
    assert not assess("微信代收招聘日入10000", [], rules, {})["reasons"]


async def test_admin_versions_idempotency_and_invalid_edits(env, client):
    initial = (await client.get("/api/settings/business-policy")).json()
    document = copy.deepcopy(initial["document"])
    document["stages"][0]["enabled"] = False
    saved = await save_policy(client, document, initial["version"], "business-version-first")
    assert await save_policy(client, document, initial["version"], "business-version-first") == saved
    stale = await client.put("/api/settings/business-policy", headers={"If-Match": initial["version"]},
        json={"document": initial["document"], "request_id": "business-version-stale"})
    assert stale.status_code == 409
    document["stages"][0]["rules"][0]["condition"] = {"op": "match", "input": "text", "pattern": "[", "mode": "search"}
    invalid = await client.put("/api/settings/business-policy", headers={"If-Match": saved["version"]},
        json={"document": document, "request_id": "business-invalid-regex"})
    assert invalid.status_code == 422
    assert (await client.get("/api/settings/business-policy")).json() == saved


@pytest.mark.parametrize("action", ["mute", "ban"])
async def test_profile_disposition_is_respected_on_join_before_any_message(env, client, action):
    from conftest import join
    from purrai.models import Group
    _, db, telegram, service = env
    async with db.transaction() as session:
        group = await session.get(Group, -1001)
        group.settings = {**group.settings, "verification_enabled": False}
    saved = (await client.get("/api/settings/business-policy")).json()
    document = saved["document"]
    stage = next(item for item in document["stages"] if item["id"] == "profile")
    stage["rules"].insert(0, arbitrary_rule("profile", action=action))
    await save_policy(client, document, saved["version"], "business-join-" + action)
    event = join()
    event["new_chat_member"]["user"]["first_name"] = "星云橙灯"
    await service.member_event(event)
    await drain(service)
    calls = [method for method, _ in telegram.calls]
    assert ("ban_chat_member" in calls) == (action == "ban")
    assert ("restrict_chat_member" in calls) == (action == "mute")
    assert "delete_message" not in calls
