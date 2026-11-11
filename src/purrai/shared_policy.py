"""Shared defaults with explicit per-group overrides and one effective rule list."""

import copy

from sqlalchemy import select

from purrai.models import Group, Rule, SharedPolicy, default_settings, uid
from purrai.serialization import row_dict
from purrai.record_versions import versioned
from purrai.business_rules import RuleSet, default_business_rules


async def policy(session):
    row = await session.get(SharedPolicy, 1)
    return row or SharedPolicy(id=1, settings=default_settings(), rules=[], business_rules=default_business_rules())


async def initial_settings(session):
    return copy.deepcopy((await policy(session)).settings)


async def effective_rules(session, group_id):
    current = await policy(session)
    common = current.rules
    local = (await session.scalars(select(Rule).where(Rule.group_id == group_id))).all()
    overrides = {row.inherited_from: row for row in local if row.inherited_from and row.customized}
    items = []
    for item in common:
        row = overrides.get(item["id"])
        items.append({**(row_dict(row) if row else item), "id": item["id"], "group_id": group_id,
                      "inherited_from": item["id"], "origin": item.get("origin", "manual"),
                      "inherited": row is None})
    for row in local:
        if row.customized and not row.inherited_from:
            items.append({**row_dict(row), "inherited": False})
    return RuleSet([versioned(item) for item in items], copy.deepcopy(current.business_rules))


async def effective_rule(session, group_id, identifier):
    items = await effective_rules(session, group_id)
    local = await session.get(Rule, identifier)
    effective_id = local.inherited_from if local and local.group_id == group_id and local.inherited_from else identifier
    return next((item for item in items if item["id"] == effective_id), None)


async def update_shared(session, settings=None, rules=None):
    current = await session.get(SharedPolicy, 1)
    if current is None:
        current = SharedPolicy(id=1, settings=default_settings(), rules=[])
        session.add(current)
    from purrai.schemas import GroupSettings

    if settings is not None:
        current.settings = GroupSettings.model_validate({**current.settings, **settings}).model_dump()
        for group in await session.scalars(select(Group)):
            overrides = set(group.setting_overrides or [])
            group.settings = {**group.settings, **{key: copy.deepcopy(value)
                              for key, value in current.settings.items() if key not in overrides}}
    if rules is not None:
        old_rules = {item["id"]: item for item in current.rules}
        old_ids = set(old_rules)
        new_rules = []
        for item in rules:
            identifier = item.get("id") or uid()
            if item.get("id") and identifier not in old_ids:
                raise ValueError("通用规则 ID 不存在，请刷新后重试")
            previous = old_rules.get(identifier, {})
            # Provenance belongs to the server, not to editable request fields.
            metadata = {key: previous[key] for key in ("origin", "learned_at", "sources") if key in previous}
            new_rules.append({**metadata, **item, "id": identifier})
        if len({item["id"] for item in new_rules}) != len(new_rules):
            raise ValueError("通用规则 ID 不能重复")
        current.rules = new_rules
    return {"settings": copy.deepcopy(current.settings), "rules": copy.deepcopy(current.rules)}


def merge_shared_changes(current, body):
    """Three-way merge only the fields/individual rules actually edited."""
    from purrai.errors import ManagementError
    from purrai.schemas import RuleInput

    proposed = [rule.model_dump() for rule in body.rules] if body.rules is not None else None
    if body.baseline is None:
        return proposed
    baseline = body.baseline
    for key, value in (body.settings or {}).items():
        old = baseline.get("settings", {}).get(key)
        if key not in baseline.get("settings", {}) or current.settings[key] not in (old, value):
            raise ManagementError(409, f"通用设置 {key} 已被修改，请刷新核对；你的草稿已保留")
    if proposed is None:
        return None

    def content(item):
        return RuleInput.model_validate(item).model_dump() if item else None

    before = {item["id"]: item for item in baseline.get("rules", [])}
    latest = {item["id"]: item for item in current.rules}
    after = {item["id"]: item for item in proposed if item.get("id")}
    merged = copy.deepcopy(latest)
    for identifier in before.keys() | after.keys():
        old, draft, actual = before.get(identifier), after.get(identifier), latest.get(identifier)
        if content(draft) == content(old):
            continue
        if content(actual) not in (content(old), content(draft)):
            raise ManagementError(409, "这条通用规则已被修改，请刷新核对；你的草稿已保留")
        if draft is None:
            merged.pop(identifier, None)
        else:
            merged[identifier] = draft
    return [*merged.values(), *[item for item in proposed if not item.get("id")]]
