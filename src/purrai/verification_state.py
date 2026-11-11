"""Verification states shared by moderation, administration and chat."""

ACTIVE = ("muting", "pending", "releasing", "removing", "failing")


def matches_action(action, verification, member):
    """A task's linked verification must still own the same member and nonce."""
    return verification is None or bool(
        verification.state in ACTIVE and member and member.generation == verification.generation
        and action.payload.get("expected_nonce") in (None, verification.nonce)
    )
