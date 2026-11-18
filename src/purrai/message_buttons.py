"""Inspect buttons attached to this message, never to a quoted/replied message."""


def inline_buttons(message):
    markup = message.get("reply_markup") or {}
    rows = markup.get("inline_keyboard", []) if isinstance(markup, dict) else []
    if not isinstance(rows, list):
        return []
    result = []
    for row in rows:
        if not isinstance(row, list):
            continue
        for button in row:
            if not isinstance(button, dict):
                continue
            link = button.get("url")
            for key in ("login_url", "web_app"):
                value = button.get(key)
                if isinstance(value, dict) and isinstance(value.get("url"), str):
                    link = value["url"]
            result.append({"text": button.get("text", ""), "url": link or ""})
    return result


def has_inline_buttons(message):
    markup = message.get("reply_markup")
    if not isinstance(markup, dict):
        return False
    rows = markup.get("inline_keyboard")
    return isinstance(rows, list) and any(
        isinstance(button, dict) and isinstance(button.get("text"), str) and bool(button["text"])
        for row in rows if isinstance(row, list) for button in row
    )
