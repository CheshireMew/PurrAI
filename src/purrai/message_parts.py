"""Own, visible Telegram content only; replies and service-message snapshots are not authorship."""
from dataclasses import dataclass


# Telegram protocol fields, not matching vocabulary. The same input feeds rules,
# links, sample/AI review, custom emoji resolution and durable evidence.
CONTENT_FIELDS = (
    "text", "caption", "entities", "caption_entities", "checklist", "poll", "venue", "game", "invoice",
    "document", "animation", "video", "audio", "sticker", "contact", "rich_message", "paid_media",
    "gift", "unique_gift", "link_preview_options", "new_chat_title", "forum_topic_created", "forum_topic_edited",
    "checklist_tasks_added", "poll_option_added",
)
MEDIA_FIELDS = ("document", "animation", "video", "audio", "sticker")
RICH_CHILDREN = ("blocks", "items", "cells", "text", "caption", "summary", "credit", "label")


@dataclass(frozen=True)
class TextPart:
    text: str
    entities: tuple[dict, ...] = ()


def rich_nodes(value):
    """Walk only rich message content, never arbitrary user/reference metadata."""
    if isinstance(value, list):
        for child in value:
            yield from rich_nodes(child)
    elif isinstance(value, dict):
        yield value
        for key in RICH_CHILDREN:
            yield from rich_nodes(value.get(key))


def rich_text(value):
    """Flatten inline formatting without inserting gaps; keep UTF-16 entities."""
    fragments, entities = [], []
    offset = 0

    def append(text):
        nonlocal offset
        if isinstance(text, str):
            fragments.append(text)
            offset += len(text.encode("utf-16-le")) // 2

    def visit(node):
        if isinstance(node, str):
            append(node)
            return
        if isinstance(node, list):
            for child in node:
                visit(child)
            return
        if not isinstance(node, dict):
            return
        kind, start = node.get("type"), offset
        if kind == "button":
            return  # Inspected once, with the other attached controls.
        if kind == "custom_emoji":
            append(node.get("alternative_text") or "\ufffc")
        elif "expression" in node:
            append(node["expression"])
        else:
            visit(node.get("text"))
        entity = {"type": kind, "offset": start, "length": offset - start}
        if kind == "url" and isinstance(node.get("url"), str):
            entity.update(type="text_link", url=node["url"])
        elif kind == "text_mention" and isinstance(node.get("user"), dict):
            entity["user"] = node["user"]
        elif kind == "custom_emoji" and node.get("custom_emoji_id"):
            entity["custom_emoji_id"] = node["custom_emoji_id"]
        elif kind in ("email_address", "phone_number", "mention", "bot_command", "cashtag", "hashtag", "bank_card_number"):
            key = "username" if kind == "mention" else kind
            target = node.get(key)
            if isinstance(target, str) and target:
                target = "@" + target.lstrip("@") if kind == "mention" else target
                # Actual entity targets must remain inspectable when their visible label differs.
                if target != "".join(fragments).encode("utf-16-le")[start * 2:offset * 2].decode("utf-16-le"):
                    append(" " + target)
        if entity["length"] or entity.get("url"):
            entities.append(entity)

    visit(value)
    return TextPart("".join(fragments), tuple(entities))


def authored_buttons(message):
    markup = message.get("reply_markup") or {}
    rows = markup.get("inline_keyboard", []) if isinstance(markup, dict) else []
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, list):
                yield from (button for button in row if isinstance(button, dict))
    for node in rich_nodes(message.get("rich_message")):
        button = node.get("button")
        if isinstance(button, dict):
            yield button
        buttons = node.get("buttons")
        if isinstance(buttons, list):
            yield from (button for button in buttons if isinstance(button, dict))


def message_media(message):
    for field in MEDIA_FIELDS:
        if isinstance(message.get(field), dict):
            yield message[field]
    poll = message.get("poll") or {}
    containers = [poll.get("media"), poll.get("explanation_media")]
    containers += [option.get("media") for option in poll.get("options", []) if isinstance(option, dict)]
    containers += list(rich_nodes(message.get("rich_message")))
    containers += (message.get("paid_media") or {}).get("paid_media", [])
    game = message.get("game") or {}
    containers.append(game)
    for container in containers:
        if isinstance(container, dict):
            for field in MEDIA_FIELDS:
                if isinstance(container.get(field), dict):
                    yield container[field]


def content_parts(message):
    result = []

    def add(value, field, entity_field=""):
        if isinstance(value, dict) and isinstance(value.get(field), str):
            entities = value.get(entity_field) or []
            result.append(TextPart(value[field], tuple(item for item in entities if isinstance(item, dict))))

    add(message, "text", "entities")
    add(message, "caption", "caption_entities")
    checklist = message.get("checklist") or {}
    add(checklist, "title", "title_entities")
    for task in checklist.get("tasks", []):
        add(task, "text", "text_entities")
    # These are the adding actor's new items. The embedded old message is only
    # a target reference and must never be scanned as that actor's own content.
    for task in (message.get("checklist_tasks_added") or {}).get("tasks", []):
        add(task, "text", "text_entities")
    add(message.get("poll_option_added"), "option_text", "option_text_entities")
    poll = message.get("poll") or {}
    for field in ("question", "description", "explanation"):
        add(poll, field, field + "_entities")
    for option in poll.get("options", []):
        add(option, "text", "text_entities")
        venue = (option.get("media") or {}).get("venue") or {}
        add(venue, "title")
        add(venue, "address")
        link = (option.get("media") or {}).get("link") or {}
        add(link, "url")
    for field in ("media", "explanation_media"):
        media = poll.get(field) or {}
        add(media.get("venue"), "title")
        add(media.get("venue"), "address")
        add(media.get("link"), "url")
    for field, keys in (("venue", ("title", "address")), ("game", ("title", "description")),
                        ("invoice", ("title", "description")), ("contact", ("first_name", "last_name", "phone_number", "vcard"))):
        for key in keys:
            add(message.get(field), key)
    add(message.get("game"), "text", "text_entities")
    for media in message_media(message):
        for field in ("file_name", "title", "performer", "emoji"):
            add(media, field)
    for field in ("gift", "unique_gift"):
        add(message.get(field), "text", "entities")
    add(message, "new_chat_title")
    for field in ("forum_topic_created", "forum_topic_edited"):
        add(message.get(field), "name")
    for node in rich_nodes(message.get("rich_message")):
        # Rich inline nodes are flattened by their enclosing text block, once.
        if "blocks" in node and "label" in node and "type" in node:
            result.append(rich_text(node["label"]))
        if node.get("type") in ("paragraph", "heading", "pre", "footer", "pullquote", "expandable_blockquote", "thinking"):
            part = rich_text(node.get("text"))
            if node["type"] in ("pre", "pullquote", "expandable_blockquote"):
                part = TextPart(part.text, part.entities + ({"type": "pre" if node["type"] == "pre" else "blockquote"},))
            result.append(part)
            if "credit" in node:
                result.append(rich_text(node["credit"]))
        elif node.get("type") == "mathematical_expression":
            add(node, "expression")
        elif "type" not in node:
            # Caption, list label and table cell text are inline RichText too.
            for field in ("text", "credit", "summary", "label"):
                if field in node:
                    result.append(rich_text(node[field]))
        elif node.get("type") in ("blockquote", "collage", "slideshow", "table", "details"):
            for field in ("credit", "summary", "caption"):
                if field in node and (not isinstance(node[field], dict) or "type" in node[field]):
                    result.append(rich_text(node[field]))
    for button in authored_buttons(message):
        result.append(rich_text(button.get("text")))
        add(button.get("copy_text"), "text")
    preview = message.get("link_preview_options") or {}
    if not preview.get("is_disabled") and isinstance(preview.get("url"), str):
        result.append(TextPart("", ({"type": "text_link", "url": preview["url"]},)))
    return result


def message_text(message):
    return "\n".join(part.text for part in content_parts(message) if part.text)


def message_entities(message):
    return [entity for part in content_parts(message) for entity in part.entities]


def stored_message_text(payload):
    """SQLite search uses exactly the same extraction as live moderation."""
    import json
    try:
        value = json.loads(payload) if isinstance(payload, str) else payload
        return message_text(value) if isinstance(value, dict) else ""
    except (TypeError, ValueError, AttributeError):
        return ""
