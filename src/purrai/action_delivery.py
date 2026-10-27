"""Durable receipts for Telegram effects that cannot safely be repeated."""

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

from purrai.action_execution import ActionPreflightError, ActionSuperseded

UNCERTAIN_DELIVERY = "发送结果未确认，为避免重复操作未重发；请先核实 Telegram 中的结果，再按需重新发起"


def uncertain_delivery(payload):
    return payload.get("delivery", {}).get("state") in ("sending", "uncertain")


async def deliver_once(call, session, action, method, **kwargs):
    previous = action.payload.get("delivery", {})
    if uncertain_delivery(action.payload):
        raise ValueError(UNCERTAIN_DELIVERY)
    if previous.get("state") == "confirmed":
        return previous["result"]

    def record(state, result=None):
        receipt = {key: result[key] for key in ("message_id", "invite_link")
                   if isinstance(result, dict) and key in result}
        action.payload = {**action.payload, "delivery": {"state": state, "method": method, "result": receipt}}

    # Read-only authorization may itself be interrupted. Record sending only
    # after those checks, immediately before the transport commits for effect I/O.
    try:
        result = await call(session, action, method, before_send=lambda: record("sending"), **kwargs)
    except (TelegramRetryAfter, TelegramBadRequest, TelegramForbiddenError, ActionPreflightError):
        record("rejected")
        raise
    except ActionSuperseded as exc:
        record("rejected" if exc.before_request else "confirmed" if exc.result is not None else "uncertain", exc.result)
        raise
    except Exception as exc:
        record("uncertain")
        raise ValueError(UNCERTAIN_DELIVERY) from exc
    record("confirmed", result)
    return result
