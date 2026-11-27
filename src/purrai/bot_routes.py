"""Dispatch Telegram buttons to their owning domain panels."""


class BotRoutes:
    def __init__(self, access, state, panels, inputs):
        self.access, self.panels, self.inputs = access, panels, inputs
        self.remember = state.remember
        self.handlers, self.prefix_handlers = panels.action_handlers, panels.action_prefixes

    async def route(self, actor, token, record, message_id):
        action, group_id, data = record["action"], record["group_id"], record["data"]
        request_id = f"tg-button:{token}"
        arguments = dict(
            actor=actor,
            group_id=group_id,
            action=action,
            data=data,
            request_id=request_id,
            message_id=message_id,
        )
        for prefix, handler, clear_prompt in self.prefix_handlers:
            if action.startswith(prefix):
                if clear_prompt:
                    await self.remember(f"bot_prompt:{actor}", None)
                return await handler(**arguments)
        if group_id is not None:
            await self.access.authorize(actor, group_id)
        if action not in ("prompt", "admin_add"):
            await self.remember(f"bot_prompt:{actor}", None)
        handler = self.handlers.get(action)
        if handler:
            return await handler(**arguments)
        if action == "group_admission":
            return await self.inputs.admission_prompt(actor)
        if action == "prompt":
            return await self.inputs.prompt(actor, group_id, data)
        if action == "confirm":
            return await self.inputs.confirmation(actor, group_id, data, message_id)
        if action == "execute":
            return await self.inputs.submit(actor, group_id, actor, data, request_id)
        if group_id is not None:
            return await self.panels.group_panel(actor, group_id, message_id)
        return await self.panels.choose_groups(actor, message_id=message_id)
