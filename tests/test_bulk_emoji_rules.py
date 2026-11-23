import copy
import json
from importlib.resources import files

import pytest
from sqlalchemy import select

from conftest import drain, message
from purrai.business_rules import BusinessEngine
from purrai.business_validation import validate_document
from purrai.message_emoji import metrics
from purrai.models import Action, Member, PromotionSource, SharedPolicy, Violation, Whitelist, now
from test_recognition_policy import save


def emoji_message(identifier=10, count=20, *, extra="", user_id=42):
    return message(identifier, "🔥" * count + extra, user_id, entities=[
        {"type": "custom_emoji", "offset": i * 2, "length": 2, "custom_emoji_id": str(1000 + i)}
        for i in range(count)])


def stage(document):
    return next(item for item in document["stages"] if item["id"] == "message_structure")


async def configure(env, client, monkeypatch, *, action="ban", learn=True):
    public = json.loads(files("purrai").joinpath("assets", "public_moderation.json").read_text(encoding="utf-8"))
    doc = (await client.get("/api/settings/business-policy")).json()["document"]
    doc["stages"].append(copy.deepcopy(stage(public["business_rules"])))
    stage(doc)["rules"][0].update(action=action, learn_emoji_packs=learn)
    await save(client, doc)
    tg = env[2]
    original = tg.call
    async def call(method, **kwargs):
        if method == "get_custom_emoji_stickers":
            tg.calls.append((method, kwargs))
            return [{"custom_emoji_id": value, "set_name": "SyntheticNewPack"} for value in kwargs["custom_emoji_ids"]]
        return await original(method, **kwargs)
    monkeypatch.setattr(tg, "call", call)
    async with env[1].transaction() as session:
        session.add(Member(group_id=-1001, user_id=42, joined_at=now(), present=True, join_observed=True))
    return doc


@pytest.mark.parametrize("count,extra,expected", [(19, "", False), (20, "", True), (20, "字" * 5, True), (20, "字" * 6, False), (36, "", True)])
async def test_count_and_visible_coverage_are_configured_and_independent_of_pack_name(env, client, monkeypatch, count, extra, expected):
    await configure(env, client, monkeypatch)
    await env[3].message(emoji_message(count=count, extra=extra))
    await drain(env[3])
    async with env[1].sessions() as session:
        violation = await session.scalar(select(Violation))
        assert bool(violation and any(v.get("learn_emoji_packs") for v in violation.reason_details.values())) is expected
        learned = [rule for rule in (await session.get(SharedPolicy, 1)).rules if rule["kind"] == "block_emoji_pack"]
        assert bool(learned) is expected
    assert any(method == "ban_chat_member" for method, _ in env[2].calls) is expected


async def test_new_pack_is_learned_once_and_one_emoji_from_it_is_blocked_next_time(env, client, monkeypatch):
    await configure(env, client, monkeypatch)
    await env[3].message(emoji_message(count=36))
    await env[3].message(emoji_message(count=36))
    await drain(env[3])
    async with env[1].sessions() as session:
        learned = [r for r in (await session.get(SharedPolicy, 1)).rules if r["kind"] == "block_emoji_pack"]
        assert len(learned) == 1 and learned[0]["value"] == "syntheticnewpack"
        assert learned[0]["penalty"] == "ban"
        assert len((await session.scalars(select(PromotionSource))).all()) == 1
        audit = await session.scalar(select(Action).where(Action.dedupe_key == "learn-emoji-packs:-1001:10"))
        assert audit.state == "succeeded" and audit.payload["result"]["emoji_packs"] == ["syntheticnewpack"]
    await env[3].message(emoji_message(11, count=1, user_id=43))
    await drain(env[3])
    async with env[1].sessions() as session:
        later = await session.scalar(select(Violation).where(Violation.message_id == 11))
        assert any("syntheticnewpack" in reason for reason in later.reasons)
    assert any(method == "ban_chat_member" and kwargs["user_id"] == 43 for method, kwargs in env[2].calls)


@pytest.mark.parametrize("exemption", ["administrator", "whitelist"])
async def test_exempt_sender_neither_punished_nor_used_to_learn_pack(env, client, monkeypatch, exemption):
    await configure(env, client, monkeypatch)
    if exemption == "administrator":
        env[2].members[(-1001, 42)] = {"status": "administrator"}
    else:
        async with env[1].transaction() as session:
            session.add(Whitelist(group_id=-1001, user_id=42, moderation=True, verification=True))
    await env[3].message(emoji_message())
    await drain(env[3])
    async with env[1].sessions() as session:
        assert not await session.scalar(select(Violation))
        assert not [r for r in (await session.get(SharedPolicy, 1)).rules if r["kind"] == "block_emoji_pack"]
    assert not any(method in ("delete_message", "restrict_chat_member", "ban_chat_member") for method, _ in env[2].calls)


async def test_editable_threshold_disable_and_stale_self_learned_pack_do_not_preserve_old_penalty(env, client, monkeypatch):
    doc = await configure(env, client, monkeypatch, action="delete")
    await env[3].message(emoji_message(10, 20))
    stage(doc)["rules"][0]["condition"]["items"][0]["minimum"] = 21
    await save(client, doc)
    await drain(env[3])
    assert not any(method == "delete_message" for method, _ in env[2].calls)
    await env[3].message(emoji_message(11, 20, user_id=43))
    # Existing pack blacklist still applies; disabling that learned rule must be respected too.
    async with env[1].transaction() as session:
        shared = await session.get(SharedPolicy, 1)
        shared.rules = [{**r, "enabled": False} for r in shared.rules]
    await drain(env[3])
    stage(doc)["rules"][0]["enabled"] = False
    await save(client, doc)
    await env[3].message(emoji_message(12, 36, user_id=44))
    await drain(env[3])
    async with env[1].sessions() as session:
        assert not await session.scalar(select(Violation).where(Violation.message_id == 12))


def test_metrics_ignore_ordinary_unicode_emoji_and_all_quoted_snapshots():
    raw = message(1, "🔥" * 40, reply_to_message=emoji_message(count=40))
    assert metrics(raw)["custom_emoji_count"] == 0
    assert metrics(raw)["custom_emoji_percent"] == 0
    own = emoji_message(count=20, extra="\n \t")
    own["reply_to_message"] = emoji_message(count=80)
    assert metrics(own) == {"text_length": 20, "custom_emoji_count": 20, "custom_emoji_percent": 100}


def test_numeric_ranges_are_generic_and_validate_field_types():
    public = json.loads(files("purrai").joinpath("assets", "public_moderation.json").read_text(encoding="utf-8"))
    doc = validate_document(public["business_rules"])
    engine = BusinessEngine(document=doc)
    assert engine.test({"op": "range", "input": "text_length", "minimum": 3, "maximum": 5}, engine.context("任意替换词"))
    stage(doc)["rules"][0]["condition"] = {"op": "range", "input": "text", "minimum": 3}
    with pytest.raises(ValueError, match="数值字段"):
        validate_document(doc)


async def test_arbitrary_rule_identity_description_and_numeric_threshold_saved_through_management(env, client, monkeypatch):
    doc = await configure(env, client, monkeypatch, action="delete")
    rule = stage(doc)["rules"][0]
    rule.update(id="arbitrary_structure_rule", title="任意结构规则", reason="任意替换判定")
    rule["condition"]["items"][0]["minimum"] = 3
    rule["condition"]["items"][1]["minimum"] = 60
    await save(client, doc)
    incoming = emoji_message(count=3)
    preview = await client.post("/api/settings/business-policy/preview", json={
        "group_id": -1001, "document": doc, "text": incoming["text"], "entities": incoming["entities"]})
    assert preview.status_code == 200, preview.text
    assert "任意替换判定" in preview.json()["after"]["reasons"]
    await env[3].message(incoming)
    await drain(env[3])
    async with env[1].sessions() as session:
        violation = await session.scalar(select(Violation))
        assert "任意替换判定" in violation.reasons
        learned = next(r for r in (await session.get(SharedPolicy, 1)).rules if r["kind"] == "block_emoji_pack")
        assert learned["penalty"] == "delete"


async def test_mistaken_verdict_can_disable_learned_pack_and_release_member_atomically(env, client, monkeypatch):
    await configure(env, client, monkeypatch)
    await env[3].message(emoji_message())
    await drain(env[3])
    async with env[1].sessions() as session:
        violation = await session.scalar(select(Violation))
    url = f"/api/groups/-1001/violations/{violation.id}/correction"
    preview = (await client.get(url)).json()
    assert len(preview["rules"]) == 1 and preview["rules"][0]["kind"] == "block_emoji_pack"
    rule = preview["rules"][0]
    result = await client.post(url, json={"reason": "管理员核实", "action": "unban", "scope": "global",
        "rule_versions": {rule["id"]: rule["global_version"]}, "request_id": "correct-emoji-verdict"})
    assert result.status_code == 200, result.text
    await drain(env[3])
    async with env[1].sessions() as session:
        assert not next(r for r in (await session.get(SharedPolicy, 1)).rules if r["id"] == rule["id"])["enabled"]
        assert (await session.scalar(select(PromotionSource))).withdrawn_at > 0
        assert (await session.get(Whitelist, (-1001, 42))).moderation
    assert env[2].members[(-1001, 42)]["status"] == "left"


async def test_saved_learning_flag_survives_older_client_and_disabled_pack_is_not_reenabled(env, client, monkeypatch):
    doc = await configure(env, client, monkeypatch)
    stage(doc)["rules"][0].pop("learn_emoji_packs")
    saved = await save(client, doc)
    assert stage(saved["document"])["rules"][0]["learn_emoji_packs"]
    await env[3].message(emoji_message())
    await drain(env[3])
    async with env[1].transaction() as session:
        shared = await session.get(SharedPolicy, 1)
        shared.rules = [{**rule, "enabled": False} for rule in shared.rules]
    await env[3].message(emoji_message(11, count=20, user_id=43))
    await drain(env[3])
    async with env[1].sessions() as session:
        learned = [r for r in (await session.get(SharedPolicy, 1)).rules if r["kind"] == "block_emoji_pack"]
        assert len(learned) == 1 and not learned[0]["enabled"]
