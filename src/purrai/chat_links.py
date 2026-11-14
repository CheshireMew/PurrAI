"""Link evidence for chat input, including editable obfuscated-word candidates."""

from purrai.chat_safety import LINKS
from purrai.rules import ai_keyword_rules
from purrai.text_matching import normalize


def input_links(text, urls=(), *, rules=()):
    explicit = [url for url in urls if url]
    found = list(explicit)
    for match in LINKS.finditer(normalize(text)):
        value = match.group().rstrip(".,，。!?！？);；:：]】}）\"'")
        # Telegram URL entities / explicit URL evidence always take precedence.
        # A guessed bare domain made only of Han characters and dots can instead
        # be an obfuscated configured keyword. No business terms live here.
        han_dotted = "." in value and all(char == "." or "\u3400" <= char <= "\u9fff" for char in value)
        if not explicit and han_dotted and ai_keyword_rules(value, rules):
            continue
        found.append(value)
    return list(dict.fromkeys(found))


def has_link(text, urls=(), *, rules=()):
    return bool(input_links(text, urls, rules=rules))
