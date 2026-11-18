"""Portable moderation settings for a fresh installation or a reviewed Git export."""
import json
import sqlite3
from importlib.resources import files
from pathlib import Path

from sqlalchemy import select

from purrai.business_validation import validate_document
from purrai.models import SharedPolicy, uid
from purrai.schemas import GroupSettings, RuleInput


def published():
    return json.loads(files("purrai").joinpath("assets/public_moderation.json").read_text(encoding="utf-8"))


def install(connection):
    profile = published()
    if profile["format"] != 1:
        raise ValueError("不支持的公开配置格式")
    table = SharedPolicy.__table__
    current = connection.execute(select(table.c.settings).where(table.c.id == 1)).scalar_one()
    settings = GroupSettings.model_validate({**current, **profile["settings"]}).model_dump()
    rules = [{**RuleInput.model_validate(row).model_dump(), "id": uid()} for row in profile["rules"]]
    connection.execute(table.update().where(table.c.id == 1).values(
        settings=settings, rules=rules, business_rules=validate_document(profile["business_rules"])))


def export_public(database_path):
    """Only export portable shared configuration. Exclude audit trails and learned targets."""
    from contextlib import closing

    with closing(sqlite3.connect(Path(database_path).resolve().as_uri() + "?mode=ro", uri=True)) as db:
        row = db.execute("SELECT settings,rules,business_rules FROM shared_policy WHERE id=1").fetchone()
    if row is None:
        raise ValueError("数据库中没有已保存的通用配置")
    settings, rules, business = map(json.loads, row)
    settings = GroupSettings.model_validate(settings).model_dump()
    allowed = published()["settings"]
    return {"format": 1, "settings": {key: settings[key] for key in allowed},
            "rules": [RuleInput.model_validate(rule).model_dump(exclude_defaults=True)
                      for rule in rules if rule["kind"] in ("keyword", "block_emoji_pack", "block_domain", "allow_domain")
                      and rule.get("origin", "manual") == "manual"],
            "business_rules": validate_document(business)}
