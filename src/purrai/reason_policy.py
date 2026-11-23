"""Editable recognition of stored verdict labels, separate from explicit actions."""
import copy
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import false, or_

from purrai.business_rules import RuleSet
from purrai.installation_defaults import reason_policy_seed
from purrai.recognition_policy import Entry


class ReasonEntry(Entry):
    mode: Literal["exact", "prefix", "contains"] = "prefix"
    value: str = Field(min_length=1, max_length=500)


class ReasonPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ban: list[ReasonEntry] = Field(default_factory=list, max_length=200)
    contact: list[ReasonEntry] = Field(default_factory=list, max_length=200)
    spam: list[ReasonEntry] = Field(default_factory=list, max_length=200)
    repeat: list[ReasonEntry] = Field(default_factory=list, max_length=200)
    advertising_stats: list[ReasonEntry] = Field(default_factory=list, max_length=200)
    spam_stats: list[ReasonEntry] = Field(default_factory=list, max_length=200)
    ai_history: list[ReasonEntry] = Field(default_factory=list, max_length=200)
    target_learning: list[ReasonEntry] = Field(default_factory=list, max_length=200)
    media_gate: list[ReasonEntry] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def unique_ids(self):
        ids = [entry.id for key in type(self).model_fields for entry in getattr(self, key)]
        if len(ids) != len(set(ids)):
            raise ValueError("判定匹配标识不能重复")
        return self


def default_reason_policy():
    return copy.deepcopy(reason_policy_seed())


def configuration(rules=None):
    return rules.business.get("reason_policy", {}) if isinstance(rules, RuleSet) else reason_policy_seed()


def matches(reason, category, rules=None):
    for item in configuration(rules).get(category, []):
        if not item["enabled"]:
            continue
        value, mode = item["value"], item["mode"]
        if (reason == value if mode == "exact" else reason.startswith(value) if mode == "prefix" else value in reason):
            return True
    return False


def details(reason):
    if not hasattr(reason, "action"):
        return {}
    result = {"action": reason.action, "contact": reason.contact}
    if getattr(reason, "learn_emoji_packs", False):
        result.update(learn_emoji_packs=True, rule_id=reason.rule_id)
    return result


def classifies(reason, category, rules=None, recorded=None):
    explicit = details(reason) or (recorded or {}).get(str(reason), {})
    if explicit and category == "ban":
        return explicit["action"] == "ban"
    if explicit and category == "contact":
        return explicit.get("contact", False)
    return matches(reason, category, rules)


def sql_matches(column, category, rules):
    conditions = []
    for item in configuration(rules).get(category, []):
        if item["enabled"]:
            value, mode = item["value"], item["mode"]
            # instr/substr are literal and case-sensitive, like the Python matcher.
            from sqlalchemy import func
            conditions.append(column == value if mode == "exact" else
                              func.substr(column, 1, len(value)) == value if mode == "prefix" else
                              func.instr(column, value) > 0)
    return or_(*conditions) if conditions else false()


def unique_reasons(reasons):
    chosen = {}
    rank = {"": 0, "exclude": 0, "delete": 1, "mute": 2, "ban": 3}
    for reason in reasons:
        previous = chosen.get(str(reason))
        if previous is None or (rank.get(getattr(reason, "action", ""), 0), getattr(reason, "contact", False)) > (
                rank.get(getattr(previous, "action", ""), 0), getattr(previous, "contact", False)):
            chosen[str(reason)] = reason
    return list(chosen.values())
