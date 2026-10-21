"""Bound temporary moderation data without discarding business history."""

from sqlalchemy import and_, delete, exists, select

from purrai.models import Fingerprint, Member, now

# Match the largest supported rule windows and the moderation query limit.
FINGERPRINT_SECONDS = 3600
FINGERPRINT_LIMIT = 500
NEWCOMER_MESSAGES = 100


def initial_fingerprint():
    # The first messages are also used when an old message is edited. Preserve
    # their original newcomer position for the member's current join, including
    # all messages belonging to those initial albums.
    return and_(
        Fingerprint.sequence.between(1, NEWCOMER_MESSAGES),
        exists().where(
            Member.group_id == Fingerprint.group_id,
            Member.user_id == Fingerprint.user_id,
            Member.generation == Fingerprint.generation,
        ),
    )


async def trim_member_fingerprints(session, group_id, user_id, generation):
    scope = (
        Fingerprint.group_id == group_id,
        Fingerprint.user_id == user_id,
        Fingerprint.generation == generation,
    )
    excess = (
        select(Fingerprint.message_id)
        .where(*scope)
        .order_by(
            Fingerprint.created_at.desc(),
            Fingerprint.message_id.desc(),
        )
        .offset(FINGERPRINT_LIMIT)
    )
    await session.execute(
        delete(Fingerprint)
        .where(
            *scope,
            Fingerprint.message_id.in_(excess),
            ~initial_fingerprint(),
        )
        .execution_options(synchronize_session=False)
    )


async def prune_expired_fingerprints(session):
    await session.execute(
        delete(Fingerprint)
        .where(
            Fingerprint.created_at < now() - FINGERPRINT_SECONDS,
            ~initial_fingerprint(),
        )
        .execution_options(synchronize_session=False)
    )
