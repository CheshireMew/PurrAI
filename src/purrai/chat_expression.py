"""Separate complete Unicode emoji sequences from the bot's outgoing text."""

import json
import re
from pathlib import Path


def emoji_pattern():
    data = json.loads((Path(__file__).parent / "assets/emoji-ranges.json").read_text(encoding="utf-8"))
    chars = "".join(chr(value) for start, end in data["emoji_ranges"] for value in range(start, end + 1)
                    if value > 127 and chr(value) not in "©®™")
    unit = (r"(?:[#*0-9]\ufe0f?\u20e3|[©®™]\ufe0f|[" + re.escape(chars)
            + r"][\ufe0e\ufe0f]?[\U0001f3fb-\U0001f3ff]?)")
    return re.compile(r"[\U0001f1e6-\U0001f1ff]{2}|" + unit
                      + r"(?:\u200d" + unit + r")*(?:[\U000e0020-\U000e007e]+\U000e007f)?")


EMOJI = emoji_pattern()


def separate_emoji(text):
    emojis = []

    def remove(match):
        emojis.append(match.group())
        return ""

    clean = EMOJI.sub(remove, text or "")
    clean = re.sub(r"[ \t]{2,}", " ", clean)
    clean = re.sub(r"[ \t]+([，。！？；：,.!?;:])", r"\1", clean).strip()
    return clean or None, emojis
