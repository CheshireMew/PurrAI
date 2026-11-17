"""Initial editable group settings, used only when a setting has never been saved."""
import copy
import json
from functools import lru_cache
from importlib.resources import files


@lru_cache(maxsize=1)
def _document():
    return json.loads(files("purrai").joinpath("assets/group_defaults.v1.json").read_text(encoding="utf-8"))


def initial_list(key):
    return copy.deepcopy(_document()[key])
