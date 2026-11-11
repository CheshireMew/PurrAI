"""Consistent sender names for moderation evidence, archives and chat records."""


def sender_name(sender, *, include_username=False, limit=500):
    name = " ".join(str(sender.get(key) or "").strip()
                    for key in ("first_name", "last_name", "title") if sender.get(key)).strip()
    username = str(sender.get("username") or "").lstrip("@")
    if include_username and username:
        name = f"{name} @{username}".strip()
    return name[:limit]
