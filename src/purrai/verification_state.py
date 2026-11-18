"""Verification states shared by moderation, administration and chat."""

from sqlalchemy import select

from purrai.models import Inbox

ACTIVE = ("muting", "pending", "checking", "releasing", "removing", "failing")


def confirm_prompt(verification, group, message_id):
    from purrai.models import now

    verification.message_id = message_id
    verification.deadline = now() + group.settings["verification_timeout"]
    if verification.flow == "join_request":
        verification.deadline = min(verification.deadline, verification.request_date + 240)
    verification.state, verification.error, verification.updated_at = "pending", "", now()


async def queued_answer(session, verification):
    return await session.scalar(select(Inbox.id).where(
        Inbox.state.in_(("pending", "failed")), Inbox.received_at > 0,
        Inbox.received_at < verification.deadline,
        Inbox.payload["callback_query"]["from"]["id"].as_integer() == verification.user_id,
        Inbox.payload["callback_query"]["data"].as_string().startswith(f"v:{verification.id}:{verification.nonce}:"),
    ).limit(1)) is not None


def matches_action(action, verification, member):
    """A task's linked verification must still own the same member and nonce."""
    return verification is None or bool(
        verification.state in ACTIVE and member and member.generation == verification.generation
        and action.payload.get("expected_nonce") in (None, verification.nonce)
    )
