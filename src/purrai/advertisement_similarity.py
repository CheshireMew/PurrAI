"""Local near-copy matching against independent samples, without domain vocabulary.

Uses Glean's character-sequence/set-overlap approach, without news facts or entities.
Asymmetric coverage allows shortened copies; new surrounding content requires review.
"""
import re
import unicodedata
from difflib import SequenceMatcher
from functools import lru_cache

from purrai.text_matching import normalize


BODY_SAMPLE = "text"


@lru_cache(maxsize=4096)
def features(value):
    value = normalize(value)
    # Variable contacts and amounts do not change a copied template's structure.
    value = re.sub(r"(?:https?://|www\.|t\.me/)[^\s<>]+", "§", value)
    value = re.sub(r"(?<![\w@])@[a-z0-9_]+", "§", value)
    value = re.sub(r"\d+(?:[.,]\d+)*", "#", value)
    value = "".join(char for char in value if char.isalnum() or char in "§#")
    return value, frozenset(value[i:i + 2] for i in range(len(value) - 1))


def quoted(value):
    return any(unicodedata.category(char) in ("Pi", "Pf") or char in '\"`「」『』>' for char in value)


def compare(value, sample, minimum_length=14, minimum_sample_coverage=0.55):
    left, a = features(value)
    right, b = features(sample)
    if min(len(left), len(right)) < minimum_length:
        return {"score": 0.0, "coverage": 0.0, "sample_coverage": 0.0, "direct": False}
    matcher = SequenceMatcher(None, left, right, autojunk=False)
    matched = sum(block.size for block in matcher.get_matching_blocks())
    coverage, sample_coverage = matched / len(left), matched / len(right)
    # Overlap of the shorter text is useful for batch ads with optional suffixes.
    overlap = len(a & b) / max(1, min(len(a), len(b)))
    score = round(0.55 * coverage + 0.45 * overlap, 6)
    # Added or replaced words can negate the same template. Normalized variable
    # contacts/numbers and shortened copies remain eligible without semantic guessing.
    additions = any(tag in ("delete", "replace") for tag, *_ in matcher.get_opcodes())
    return {"score": score, "coverage": round(coverage, 6), "sample_coverage": round(sample_coverage, 6),
            "direct": not additions and sample_coverage >= minimum_sample_coverage and not quoted(value)}


def rank(value, samples, settings, *, context_sensitive=False, scope=BODY_SAMPLE):
    if not settings.similarity_enabled:
        return None
    best = None
    for sample in samples:
        if not sample.enabled or sample.scope != scope:
            continue
        result = compare(value, sample.text, settings.minimum_length, settings.minimum_sample_coverage)
        result.update(sample_id=sample.id, sample_version=sample.version,
                      matched=scope == BODY_SAMPLE and result["direct"] and not context_sensitive
                      and result["score"] >= settings.threshold)
        if best is None or (result["matched"], result["score"]) > (best["matched"], best["score"]):
            best = result
    return best
