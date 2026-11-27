"""Shared message eligibility queries, independent of conversation policy."""

from sqlalchemy import func, or_, select

from purrai.models import Action, ArchivedMessage, Verification, Violation
from purrai.verification_state import ACTIVE as ACTIVE_VERIFICATIONS


def blocked_message(group_id, message_id, sender_id):
    """The same current moderation/deletion facts gate both inputs and history."""
    return or_(
        select(Verification.id)
        .where(
            Verification.group_id == group_id,
            Verification.user_id == sender_id,
            Verification.state.in_(ACTIVE_VERIFICATIONS),
        )
        .exists(),
        select(Violation.id)
        .where(
            Violation.group_id == group_id,
            Violation.message_id == message_id,
        )
        .exists(),
        select(Action.id)
        .where(
            Action.group_id == group_id,
            Action.kind == "delete",
            Action.state != "cancelled",
            func.coalesce(Action.payload["chat_id"].as_integer(), Action.group_id) == group_id,
            Action.target_message_id == message_id,
        )
        .exists(),
        select(ArchivedMessage.id)
        .where(
            ArchivedMessage.group_id == group_id,
            ArchivedMessage.message_id == message_id,
            ArchivedMessage.deletion_state != "not_requested",
        )
        .exists(),
    )
