"""Classify structured channel references without guessing from message text."""

import json

from purrai.telegram_links import TelegramLink, allowed_link

CHANNEL_PREFIX = "外部频道广告："


def stored_channel_sources(snapshot):
    try:
        value = json.loads(snapshot)
        sources = value.get("channel_sources", [])
        return sources if isinstance(sources, list) else []
    except (ValueError, TypeError, AttributeError):
        return []


def channel_sources(message):
    """Only the message author's own cross-chat reference or forward counts.

    A normal same-group reply may contain a channel post in reply_to_message;
    that nested post is context, not a channel shared by the replying member.
    """
    result = []

    def add(chat, kind, post=0):
        identifier = chat.get("id")
        if chat.get("type") != "channel" or type(identifier) is not int or identifier >= 0:
            return
        source = {"id": identifier, "username": chat.get("username", ""), "kind": kind,
                  "post": post if type(post) is int and post > 0 else 0}
        if source not in result:
            result.append(source)

    external = message.get("external_reply") or {}
    origin = external.get("origin") or {}
    add(origin.get("chat") or origin.get("sender_chat") or external.get("chat") or {},
        "reference", origin.get("message_id") or external.get("message_id"))
    if not message.get("is_automatic_forward"):
        origin = message.get("forward_origin") or {}
        add(origin.get("chat") or origin.get("sender_chat") or {}, "forward", origin.get("message_id"))
        add(message.get("forward_from_chat") or {}, "forward", message.get("forward_from_message_id"))
    return result


def external_channel_reasons(sources, group_id, settings, rules=(), *, linked_channel_id=0):
    reasons = []
    for source in sources:
        identifier = source.get("id")
        if type(identifier) is not int or identifier >= 0 or identifier in (group_id, linked_channel_id):
            continue
        post = str(source.get("post") or "")
        username = source.get("username", "").lower().lstrip("@")
        private = TelegramLink("private_post", str(identifier), post)
        public = TelegramLink("public", username, post) if username else None
        if allowed_link(private, settings, rules, group_id=group_id) or (
            public and allowed_link(public, settings, rules, group_id=group_id)
        ):
            continue
        kind = "引用" if source.get("kind") == "reference" else "转发"
        reasons.append(f"{CHANNEL_PREFIX}{kind}频道 {identifier}" + (f" (@{username})" if username else ""))
    return list(dict.fromkeys(reasons))
