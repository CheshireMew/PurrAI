"""Bound temporary moderation data without discarding business history."""

from sqlalchemy import and_, delete, exists, select

from purrai.models import Fingerprint, Inbox, LoginAttempt, Member, Meta, Session, now

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


async def prune_temporary_state(session, limit=250):
    """Small writer batches; pending/failed inputs and audit actions are retained."""
    cutoff = now()
    removed = 0
    for model, key, condition in (
        (Meta, Meta.key, and_(Meta.expires_at > 0, Meta.expires_at <= cutoff)),
        (Session, Session.id, Session.expires_at <= cutoff),
        (LoginAttempt, LoginAttempt.state, LoginAttempt.expires_at <= cutoff),
        (Inbox, Inbox.id, and_(Inbox.state == "done", Inbox.completed_at > 0,
                              Inbox.completed_at <= cutoff - 7 * 86400)),
    ):
        identifiers = select(key).where(condition).order_by(key).limit(limit)
        result = await session.execute(delete(model).where(key.in_(identifiers)).execution_options(synchronize_session=False))
        removed += max(0, result.rowcount)
    return removed
