"""Read-only original message references for upgraded groups."""

import json

from purrai.models import Meta


async def historical_source(session, group_id, message_id):
    if not -2147483647 <= message_id < 0:
        return {}
    marker = await session.get(Meta, f"group-migration:{group_id}")
    return {"origin_group_id": json.loads(marker.value)["old_id"], "origin_message_id": -message_id} if marker else {}
