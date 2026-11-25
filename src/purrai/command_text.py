"""Script equivalence for configured commands; never rewrites stored messages or URLs."""
from functools import lru_cache
import unicodedata

from opencc import OpenCC


@lru_cache(maxsize=1)
def converter():
    return OpenCC("t2s")


def normalized_command(value):
    return converter().convert(unicodedata.normalize("NFKC", value).strip().casefold())
