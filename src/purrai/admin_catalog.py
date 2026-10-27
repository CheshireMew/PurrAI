"""The administration contract consumed by both Telegram and the web console."""

from purrai.admin_labels import BOOLEANS, FAILURE_ACTIONS, NUMBERS, OPERATIONS, STATE
from purrai.schemas import GroupSettings
from purrai.verification_state import ACTIVE


def admin_catalog():
    return {
        "settings": {
            "defaults": GroupSettings().model_dump(),
            "fields": GroupSettings.model_json_schema()["properties"],
            "numbers": NUMBERS,
            "booleans": BOOLEANS,
        },
        "states": STATE,
        "operations": OPERATIONS,
        "failure_actions": FAILURE_ACTIONS,
        "active_verifications": list(ACTIVE),
    }
