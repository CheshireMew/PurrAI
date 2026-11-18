"""Editable recognition data; runtime snapshots never merge installation defaults."""
import copy
import re
from functools import lru_cache

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from purrai.business_rules import RuleSet
from purrai.installation_defaults import recognition_seed


class Entry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    title: str = Field(min_length=1, max_length=160)
    enabled: bool = True


class PatternEntry(Entry):
    pattern: str = Field(min_length=1, max_length=4000)

    @field_validator("pattern")
    @classmethod
    def valid_pattern(cls, value):
        try:
            compiled(value)
        except re.error as exc:
            raise ValueError(f"匹配表达式无效：{exc}") from exc
        return value


class ValueEntry(Entry):
    value: str = Field(min_length=1, max_length=250)


class RecognitionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    utility_patterns: list[PatternEntry] = Field(default_factory=list, max_length=200)
    candidate_patterns: list[PatternEntry] = Field(default_factory=list, max_length=200)
    other_bot_patterns: list[PatternEntry] = Field(default_factory=list, max_length=200)
    output_patterns: list[PatternEntry] = Field(default_factory=list, max_length=200)
    phone_prefixes: list[ValueEntry] = Field(default_factory=list, max_length=200)
    shortener_domains: list[ValueEntry] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def valid_entries(self):
        identifiers = [item.id for name in type(self).model_fields for item in getattr(self, name)]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("识别规则标识不能重复")
        for item in self.shortener_domains:
            value = item.value.lower()
            if "." not in value or not re.fullmatch(r"[a-z0-9.-]+", value):
                raise ValueError("短链接服务请填写域名，不含协议或路径")
            item.value = value
        return self


@lru_cache(maxsize=512)
def compiled(pattern):
    return re.compile(pattern, re.I)


def default_recognition():
    return copy.deepcopy(recognition_seed())


def configuration(rules=None):
    # Live entry points pass their saved RuleSet, even when every list is empty.
    return rules.business.get("recognition", {}) if isinstance(rules, RuleSet) else recognition_seed()


def matches(rules, collection, text, *, search=False):
    for item in configuration(rules).get(collection, []):
        if item["enabled"] and getattr(compiled(item["pattern"]), "search" if search else "fullmatch")(text):
            return True
    return False


def values(rules, collection):
    return [item["value"] for item in configuration(rules).get(collection, []) if item["enabled"]]


def output_reasons(rules, text):
    return [item["title"] for item in configuration(rules).get("output_patterns", [])
            if item["enabled"] and compiled(item["pattern"]).search(text)]
