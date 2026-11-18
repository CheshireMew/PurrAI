"""Resolve Telegram's sender identity, including guest-bot callers."""


def message_sender(message):
    if message.get("sender_chat"):
        return message["sender_chat"]
    sender = message.get("from") or {}
    caller_chat = message.get("guest_bot_caller_chat") or {}
    if sender.get("is_bot") is True and type(caller_chat.get("id")) is int and caller_chat["id"] < 0:
        return caller_chat
    caller = message.get("guest_bot_caller_user") or {}
    if (sender.get("is_bot") is True and caller.get("is_bot") is False
            and type(caller.get("id")) is int and caller["id"] > 0):
        return caller
    return sender


def sender_chat(message):
    sender = message_sender(message)
    return sender if type(sender.get("id")) is int and sender["id"] < 0 else None


def display_name(user, fallback="未命名成员"):
    name = " ".join(str(user.get(key) or "").strip() for key in ("first_name", "last_name", "title")).strip()
    return name or ("@" + user["username"] if user.get("username") else fallback)


def profile_name(user):
    name = " ".join(str(user.get(key) or "").strip() for key in ("first_name", "last_name", "title")).strip()
    return " ".join(filter(None, [name, "@" + user["username"] if user.get("username") else ""]))[:500]
