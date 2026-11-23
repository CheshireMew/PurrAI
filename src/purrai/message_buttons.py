"""Inspect buttons attached to this message, never to a quoted/replied message."""
from purrai.message_parts import authored_buttons, rich_text


def inline_buttons(message):
    result = []
    for button in authored_buttons(message):
        link = button.get("url")
        for key in ("login_url", "web_app"):
            value = button.get(key)
            if isinstance(value, dict) and isinstance(value.get("url"), str):
                link = value["url"]
        result.append({"text": rich_text(button.get("text")).text, "url": link or ""})
    return result


def has_inline_buttons(message):
    return any(button["text"] for button in inline_buttons(message))
