"""Read actual task outcomes, including indexed batch progress."""

from sqlalchemy import func, select

from purrai.models import Action
from purrai.serialization import row_dict


async def action_result(session, action):
    result = row_dict(action)
    if action.kind != "batch_release":
        return result
    counts = dict(
        (
            await session.execute(
                select(Action.state, func.count()).where(Action.batch_id == action.id).group_by(Action.state)
            )
        ).all()
    )
    total = len(action.payload.get("action_ids", []))
    missing = max(0, total - sum(counts.values()))
    summary = {state: counts.get(state, 0) for state in ("pending", "succeeded", "failed", "cancelled")}
    summary["failed"] += missing
    summary["total"] = total
    result["summary"] = summary
    result["state"] = (
        "pending"
        if summary["pending"]
        else "failed"
        if summary["failed"]
        else "cancelled"
        if total and summary["cancelled"] == total
        else "succeeded"
    )
    if summary["failed"]:
        error = await session.scalar(
            select(Action.error)
            .where(Action.batch_id == action.id, Action.state == "failed")
            .order_by(Action.created_at, Action.id)
            .limit(1)
        )
        result["error"] = f"{summary['failed']} 个任务失败" + (
            f"：{error}" if error else "（任务记录缺失）" if missing else ""
        )
    else:
        result["error"] = ""
    return result
