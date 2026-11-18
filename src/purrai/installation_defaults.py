"""Read installation data. Saved group and business policies are never merged here."""
import copy
import json
from functools import lru_cache
from importlib.resources import files


@lru_cache(maxsize=4)
def _document(filename):
    return json.loads(files("purrai").joinpath("assets", filename).read_text(encoding="utf-8"))


def initial_list(key):
    return copy.deepcopy(_document("group_defaults.v1.json")[key])


def seed_document():
    return _document("business_rules.v2.json")


def recognition_seed():
    return _document("recognition_policy.v1.json")


def reason_policy_seed():
    return _document("reason_policy.v1.json")


def default_business_rules():
    return copy.deepcopy({**seed_document(), "recognition": recognition_seed(), "reason_policy": reason_policy_seed()})
