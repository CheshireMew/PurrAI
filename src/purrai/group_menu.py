"""Persisted menu configuration, exact text routing and durable keyboard delivery."""
from pathlib import Path

from purrai.action_delivery import deliver_once
from purrai.action_execution import ActionSuperseded
from purrai.group_menu_schemas import MenuConfig, menu_key
from purrai.models import Action, GroupMenu


def default_menu():
    return MenuConfig.model_validate_json(
        (Path(__file__).parent / "assets" / "group_menu.v1.json").read_text(encoding="utf-8")).model_dump()


async def menu_document(session, group_id):
    row = await session.get(GroupMenu, group_id)
    delivery = await session.get(Action, row.last_action_id) if row and row.last_action_id else None
    return {"config": row.config if row else default_menu(), "version": row.version if row else 0,
            "active": bool(row and row.active_config is not None),
            "needs_update": bool(row and row.active_config is not None and row.active_config != row.config),
            "delivery": ({"id": delivery.id, "state": delivery.state, "error": delivery.error,
                          "operation": delivery.payload["operation"]} if delivery else None)}


async def matched_action(session, group_id, value):
    row = await session.get(GroupMenu, group_id)
    if row and row.active_config is not None:
        key = menu_key(value)
        for buttons in row.active_config["rows"]:
            for button in buttons:
                if menu_key(button["text"]) == key:
                    return button["action"]


def keyboard(config):
    return {"keyboard": [[{"text": button["text"],
            **({"style": button["style"]} if button["style"] != "default" else {})}
            for button in row] for row in config["rows"]],
            "resize_keyboard": True, "is_persistent": True, "one_time_keyboard": False}


async def send_menu(call, session, action, group):
    row = await session.get(GroupMenu, action.group_id)
    publish = action.payload["operation"] == "publish"

    def current():
        if (not group or not group.approved or not row or row.last_action_id != action.id
                or publish and row.version != action.payload["version"]):
            raise ActionSuperseded(reason="菜单设置或操作已更新，取消旧发送任务", before_request=True)

    current()

    async def guarded_call(session, action, method, *, before_send, **options):
        def before():
            # Actor authorization can release the writer; check again after it returns.
            current()
            before_send()
        return await call(session, action, method, before_send=before, **options)

    config = row.config if publish else None
    await deliver_once(guarded_call, session, action, "send_message", chat_id=group.id,
        text="群功能菜单" if publish else "底部菜单已清除。",
        reply_markup=keyboard(config) if publish else {"remove_keyboard": True})
    row.active_config = config
