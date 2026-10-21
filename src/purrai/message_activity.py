"""Bounded message fingerprints and repeat, similarity and burst statistics."""

from dataclasses import dataclass

from sqlalchemy import select

from purrai.models import (
    Fingerprint,
    now,
)
from purrai.rules import content_digest, is_similar, similarity_signature
from purrai.state_retention import trim_member_fingerprints


@dataclass(frozen=True)
class MessageActivity:
    newcomer: bool
    repeats: int
    similar: int
    burst: int


async def observe_activity(session, group, member, message, text, edited):
    group_id = group.id
    user_id = (message.get("sender_chat") or message["from"])["id"]
    generation = member.generation if member else ""
    existing = await session.get(Fingerprint, (group_id, user_id, message["message_id"]))
    batch_key = (
        "album:" + str(message["media_group_id"])
        if message.get("media_group_id")
        else f"m:{message['message_id']}"
    )
    if existing is None:
        album = await session.scalar(
            select(Fingerprint).where(
                Fingerprint.group_id == group_id,
                Fingerprint.user_id == user_id,
                Fingerprint.generation == generation,
                Fingerprint.batch_key == batch_key,
            )
        )
        if member and (member.passed_at or member.joined_at) and album is None and not edited:
            member.observed_messages += 1
        existing = Fingerprint(
            group_id=group_id,
            user_id=user_id,
            message_id=message["message_id"],
            generation=generation,
            sequence=album.sequence
            if album
            else (0 if edited else member.observed_messages)
            if member
            else 0,
            batch_key=batch_key,
            created_at=message["date"],
        )
        session.add(existing)
    existing.digest = content_digest(text) if text else ""
    existing.similarity_hash, existing.similarity_length = similarity_signature(text)
    await session.flush()
    await trim_member_fingerprints(session, group_id, user_id, generation)
    window = max(
        group.settings["repeat_window"],
        group.settings.get("similar_window", 60),
        group.settings.get("burst_window", 10),
    )
    recent = (
        await session.scalars(
            select(Fingerprint)
            .where(
                Fingerprint.group_id == group_id,
                Fingerprint.user_id == user_id,
                Fingerprint.generation == generation,
                Fingerprint.created_at >= now() - window,
            )
            .order_by(Fingerprint.created_at.desc())
            .limit(500)
        )
    ).all()
    repeats = sum(
        bool(existing.digest)
        and row.digest == existing.digest
        and row.created_at >= now() - group.settings["repeat_window"]
        for row in recent
    )
    similar = sum(
        row.similarity_length >= group.settings.get("similar_min_length", 12)
        and existing.similarity_length >= group.settings.get("similar_min_length", 12)
        and row.created_at >= now() - group.settings.get("similar_window", 60)
        and is_similar(
            existing.similarity_hash,
            existing.similarity_length,
            row.similarity_hash,
            row.similarity_length,
            group.settings.get("similar_threshold", 85),
        )
        for row in recent
    )
    burst = len(
        {row.batch_key for row in recent if row.created_at >= now() - group.settings.get("burst_window", 10)}
    )
    newcomer = bool(
        member
        and (member.passed_at or member.joined_at)
        and (
            now() - (member.passed_at or member.joined_at) < group.settings["newcomer_seconds"]
            or 0 < existing.sequence <= group.settings.get("newcomer_messages", 3)
        )
    )
    return MessageActivity(newcomer, repeats, similar, burst)
