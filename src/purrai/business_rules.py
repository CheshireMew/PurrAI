"""Data-driven text policy evaluation shared by moderation and chat exclusion."""

import copy
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files

from purrai.text_matching import normalize
from purrai.advertising_constants import AD_PREFIX, is_ban_reason


@lru_cache(maxsize=1)
def seed_document():
    # An installation seed, never merged over a saved policy or used as a fallback
    # for a disabled/deleted rule. Database copies are independently editable.
    return json.loads(files("purrai").joinpath("assets/business_rules.v1.json").read_text(encoding="utf-8"))


def default_business_rules():
    return copy.deepcopy(seed_document())


class RuleSet(list):
    """Ordinary rules and the business-policy snapshot used in the same evaluation."""

    def __init__(self, items=(), business=None):
        super().__init__(items)
        self.business = business if business is not None else default_business_rules()

    def __eq__(self, other):
        same = super().__eq__(other)
        return NotImplemented if same is NotImplemented else same and (not isinstance(other, RuleSet) or self.business == other.business)

    def __ne__(self, other):
        same = self.__eq__(other)
        return NotImplemented if same is NotImplemented else not same


def with_rules(original, items):
    return RuleSet(items, document_for(original))


def document_for(rules=None):
    # Standalone pure matching calls retain the installation defaults. Every live
    # entry point loads a RuleSet, including when its saved policy has zero rules.
    return rules.business if isinstance(rules, RuleSet) else seed_document()


@lru_cache(maxsize=512)
def compiled(pattern):
    return re.compile(pattern)


def transform(value, steps):
    for step in steps:
        operation = step["op"]
        if operation == "normalize":
            value = normalize(value)
        elif operation == "compact":
            value = "".join(char for char in value if char.isalnum())
        elif operation == "translate":
            value = value.translate(str.maketrans(step["value"]))
        elif operation == "replace":
            value = compiled(step["pattern"]).sub(step["value"], value)
    return value


class BusinessReason(str):
    def __new__(cls, value, action="", rule_id=""):
        # Keep disposition unambiguous after JSON/SQLite round trips. These are
        # existing reason categories, not advertising vocabulary or conditions.
        if action == "ban" and not is_ban_reason(value):
            value = AD_PREFIX + value
        elif action and action != "ban" and is_ban_reason(value):
            value = ("规则禁言：" if action == "mute" else "规则删除：") + value.split("：", 1)[-1]
        result = super().__new__(cls, value)
        result.action, result.rule_id = action, rule_id
        return result


@dataclass(frozen=True)
class Hit:
    id: str
    reason: BusinessReason
    action: str


class Context:
    def __init__(self, engine, values, cache=None):
        self.engine, self.values, self.cache = engine, values, cache or {}

    def field(self, name):
        if name in self.values:
            return self.values[name]
        if name not in self.cache:
            view = self.engine.views[name]
            self.cache[name] = transform(self.field(view["source"]), view["steps"])
        return self.cache[name]

    def child(self, **values):
        return Context(self.engine, {**self.values, **values}, {} if "text" in values else self.cache.copy())


class BusinessEngine:
    def __init__(self, rules=None, *, document=None):
        self.document = document if document is not None else document_for(rules)
        self.views = {item["id"]: item for item in self.document["views"]}
        self.conditions = {item["id"]: item for item in self.document["conditions"]}
        self.stages = {item["id"]: item for item in self.document["stages"]}

    def context(self, text, *, source_titles=(), direct=False, urls=()):
        return Context(self, {"text": text, "source_titles": source_titles, "direct": direct, "urls": urls})

    def view(self, identifier, text):
        return self.context(text).field(identifier)

    def condition(self, identifier, text):
        return self.test({"op": "ref", "ref": identifier}, self.context(text))

    def test(self, expression, context):
        operation = expression["op"]
        if operation == "always":
            return True
        if operation == "all":
            return all(self.test(item, context) for item in expression["items"])
        if operation == "any":
            return any(self.test(item, context) for item in expression["items"])
        if operation == "not":
            return not self.test(expression["item"], context)
        if operation == "ref":
            item = self.conditions[expression["ref"]]
            if not item["enabled"]:
                return False
            target = context.child(text=context.field(expression["input"])) if "input" in expression else context
            return self.test(item["condition"], target)
        value = context.field(expression["input"])
        if operation == "empty":
            return not value
        if operation == "truthy":
            return bool(value)
        if operation == "equals":
            return value == expression["value"]
        if operation == "each":
            values = compiled(expression["split"]).split(value) if "split" in expression else value
            return any(self.test(expression["condition"], context.child(**{
                "$item": transform(item, expression.get("steps", []))})) for item in values)
        pattern, mode = compiled(expression["pattern"]), expression["mode"]
        hits = pattern.finditer(value) if mode == "find" else [getattr(pattern, mode)(value)]
        for hit in hits:
            if hit is None:
                continue
            if "where" not in expression:
                return True
            start = hit.start()
            prefix_start = max(0, start - expression["prefix_chars"]) if expression.get("prefix_chars") else 0
            child = context.child(**{"$match": hit.group(), "$prefix": value[prefix_start:start], "$start": start})
            if self.test(expression["where"], child):
                return True
        return False

    def run(self, stage, text, *, source_titles=(), direct=False, urls=()):
        return self._run(stage, self.context(text, source_titles=source_titles, direct=direct, urls=urls))

    def _run(self, identifier, context):
        stage = self.stages[identifier]
        if not stage["enabled"]:
            return []
        found = []
        for previous in stage["before"]:
            found.extend(self._run(previous, context))
            if found and stage["first_match"]:
                return found
        if not self.test(stage["condition"], context):
            return found
        for rule in stage["rules"]:
            if not rule["enabled"] or not self.test(rule["condition"], context):
                continue
            reason = rule["reason"] + str(context.field(rule["reason_field"])) if rule.get("reason_field") else rule["reason"]
            found.append(Hit(rule["id"], BusinessReason(reason, rule["action"], rule["id"]), rule["action"]))
            if stage["first_match"]:
                break
        return found
