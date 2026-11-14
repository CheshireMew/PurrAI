"""Inspect buttons attached to this message, never to a quoted/replied message."""


def has_inline_buttons(message):
    markup = message.get("reply_markup")
    if not isinstance(markup, dict):
        return False
    rows = markup.get("inline_keyboard")
    return isinstance(rows, list) and any(
        isinstance(button, dict) and isinstance(button.get("text"), str) and bool(button["text"])
        for row in rows if isinstance(row, list) for button in row
    )
