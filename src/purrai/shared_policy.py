"""Shared defaults with explicit per-group overrides and one effective rule list."""

import copy

from sqlalchemy import select

from purrai.models import Group, Rule, SharedPolicy, default_settings, uid
from purrai.serialization import row_dict


async def policy(session):
    row = await session.get(SharedPolicy, 1)
    return row or SharedPolicy(id=1, settings=default_settings(), rules=[])


async def initial_settings(session):
    return copy.deepcopy((await policy(session)).settings)


async def effective_rules(session, group_id):
    common = (await policy(session)).rules
    local = (await session.scalars(select(Rule).where(Rule.group_id == group_id))).all()
    overrides = {row.inherited_from: row for row in local if row.inherited_from and row.customized}
    items = []
    for item in common:
        row = overrides.get(item["id"])
        items.append({**(row_dict(row) if row else item), "id": item["id"], "group_id": group_id,
                      "inherited": row is None})
    for row in local:
        if row.customized and not row.inherited_from:
            items.append({**row_dict(row), "inherited": False})
    return items


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
        old_ids = {item["id"] for item in current.rules}
        new_rules = []
        for item in rules:
            identifier = item.get("id") or uid()
            if item.get("id") and identifier not in old_ids:
                raise ValueError("通用规则 ID 不存在，请刷新后重试")
            new_rules.append({**item, "id": identifier})
        if len({item["id"] for item in new_rules}) != len(new_rules):
            raise ValueError("通用规则 ID 不能重复")
        current.rules = new_rules
    return {"settings": copy.deepcopy(current.settings), "rules": copy.deepcopy(current.rules)}
