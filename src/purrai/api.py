from typing import Annotated

from fastapi import Depends, Query, Request
from fastapi.responses import StreamingResponse

from purrai.admin_catalog import admin_catalog
from purrai.auth import Identity, group_ids, identity, root_identity
from purrai.schemas import (
    ActionInput,
    AdminInput,
    ArchiveFilter,
    ArchiveQuery,
    EmojiPackChange,
    GroupAccessInput,
    GroupAdmissionInput,
    PreviewInput,
    RuleInput,
    SettingsPatch,
    SharedPolicyPatch,
    WhitelistInput,
)


def install_api(app):
    @app.get("/api/settings/defaults")
    async def shared_policy(request: Request, user: Identity = Depends(root_identity)):
        return await request.app.state.management.configuration.shared(user.id)

    @app.patch("/api/settings/defaults")
    async def update_shared_policy(body: SharedPolicyPatch, request: Request, user: Identity = Depends(root_identity)):
        return await request.app.state.management.configuration.shared(user.id, body)

    @app.get("/api/me")
    async def me(request: Request, user: Identity = Depends(identity)):
        return {
            "id": user.id,
            "root": user.root,
            "csrf": user.csrf,
            "group_ids": await group_ids(request, user),
        }

    @app.get("/api/catalog")
    async def catalog(user: Identity = Depends(identity)):
        return admin_catalog()

    @app.get("/api/groups")
    async def groups(request: Request, user: Identity = Depends(identity)):
        return await request.app.state.management.groups(user.id)

    @app.post("/api/groups/{identifier}/access")
    async def group_access(
        identifier: int, body: GroupAccessInput, request: Request, user: Identity = Depends(root_identity)
    ):
        return await request.app.state.management.group_access(user.id, identifier, body.approved)

    @app.post("/api/groups")
    async def admit_group(
        body: GroupAdmissionInput, request: Request, user: Identity = Depends(root_identity)
    ):
        return await request.app.state.management.admit_group(user.id, body.group_id)

    @app.post("/api/groups/{identifier}/activate")
    async def activate(identifier: int, request: Request, user: Identity = Depends(identity)):
        return await request.app.state.management.permissions(user.id, identifier, activate=True)

    @app.post("/api/groups/{identifier}/permissions")
    async def check_permissions(identifier: int, request: Request, user: Identity = Depends(identity)):
        result = await request.app.state.management.permissions(user.id, identifier)
        return result["permissions"]

    @app.post("/api/groups/{identifier}/join-link")
    async def join_link(
        identifier: int, body: ActionInput, request: Request, user: Identity = Depends(identity)
    ):
        return await request.app.state.management.join_link(user.id, identifier, body.request_id)

    @app.patch("/api/groups/{identifier}/settings")
    async def update_settings(
        identifier: int, body: SettingsPatch, request: Request, user: Identity = Depends(identity)
    ):
        return await request.app.state.management.settings_patch(user.id, identifier, body)

    @app.post("/api/groups/{identifier}/emoji-status-packs")
    async def change_status_packs(
        identifier: int, body: EmojiPackChange, request: Request, user: Identity = Depends(identity)
    ):
        return await request.app.state.management.change_status_packs(user.id, identifier, body)

    @app.get("/api/dashboard")
    async def dashboard(request: Request, user: Identity = Depends(identity)):
        return await request.app.state.management.queries.dashboard(user.id)

    @app.get("/api/groups/{identifier}/chat")
    async def chat_status(identifier: int, request: Request, user: Identity = Depends(identity)):
        return await request.app.state.management.chat_status(user.id, identifier)

    @app.post("/api/groups/{identifier}/chat/reset")
    async def reset_chat(identifier: int, request: Request, user: Identity = Depends(identity)):
        from purrai.models import uid

        return await request.app.state.management.reset_chat(user.id, identifier, uid())

    @app.get("/api/groups/{identifier}/messages")
    async def archived_messages(
        identifier: int,
        request: Request,
        filters: Annotated[ArchiveQuery, Query()],
        user: Identity = Depends(identity),
    ):
        return await request.app.state.management.archived_messages(
            user.id, identifier, filters, filters.page_number, filters.size
        )

    @app.get("/api/groups/{identifier}/messages/export")
    async def export_messages(
        identifier: int,
        request: Request,
        filters: Annotated[ArchiveFilter, Query()],
        user: Identity = Depends(identity),
    ):
        stream = await request.app.state.management.archives.export_stream(user.id, identifier, filters)
        return StreamingResponse(
            stream,
            media_type="application/x-ndjson",
            headers={
                "Content-Disposition": f'attachment; filename="purrai-{identifier}-messages.jsonl"',
                "Cache-Control": "no-store",
            },
        )

    @app.get("/api/groups/{identifier}/messages/{message_id}")
    async def archived_message(
        identifier: int,
        message_id: str,
        request: Request,
        user: Identity = Depends(identity),
    ):
        return await request.app.state.management.archived_message(user.id, identifier, message_id)

    @app.get("/api/groups/{identifier}/verifications")
    async def verifications(
        identifier: int,
        request: Request,
        state: str = "",
        user_id: int | None = None,
        after: float = 0,
        before: float = 0,
        page_number: int = Query(1, ge=1),
        size: int = Query(20, ge=1, le=100),
        user: Identity = Depends(identity),
    ):
        return await request.app.state.management.queries.page(
            user.id,
            identifier,
            "verification",
            state=state,
            user_id=user_id,
            after=after,
            before=before,
            page_number=page_number,
            size=size,
        )

    @app.post("/api/verifications/{identifier}/actions")
    async def verification_action(
        identifier: str, body: ActionInput, request: Request, user: Identity = Depends(identity)
    ):
        return await request.app.state.management.verification_action(user.id, identifier, body)

    @app.post("/api/groups/{identifier}/release-pending")
    async def release_pending(
        identifier: int, body: ActionInput, request: Request, user: Identity = Depends(identity)
    ):
        return await request.app.state.management.release_pending(user.id, identifier, body)

    @app.get("/api/groups/{identifier}/actions")
    async def group_actions(
        identifier: int,
        request: Request,
        state: str = "",
        page_number: int = Query(1, ge=1),
        size: int = Query(20, ge=1, le=100),
        user: Identity = Depends(identity),
    ):
        return await request.app.state.management.queries.page(
            user.id,
            identifier,
            "action",
            state=state,
            page_number=page_number,
            size=size,
            manual_only=True,
        )

    @app.get("/api/groups/{identifier}/rules")
    async def rules(identifier: int, request: Request, user: Identity = Depends(identity)):
        return (await request.app.state.management.queries.page(user.id, identifier, "rule", size=None))[
            "items"
        ]

    @app.post("/api/groups/{identifier}/rules")
    async def add_rule(
        identifier: int, body: RuleInput, request: Request, user: Identity = Depends(identity)
    ):
        return await request.app.state.management.rule(user.id, identifier, body)

    @app.put("/api/groups/{identifier}/rules/{rule_id}")
    async def edit_rule(
        identifier: int, rule_id: str, body: RuleInput, request: Request, user: Identity = Depends(identity)
    ):
        return await request.app.state.management.rule(user.id, identifier, body, rule_id)

    @app.post("/api/groups/{identifier}/rules/preview")
    async def preview(
        identifier: int, body: PreviewInput, request: Request, user: Identity = Depends(identity)
    ):
        return await request.app.state.management.preview(user.id, identifier, body)

    @app.post("/api/groups/{identifier}/rules/{rule_id}/inherit")
    async def inherit_rule(identifier: int, rule_id: str, request: Request, user: Identity = Depends(identity)):
        return await request.app.state.management.rules.inherit(user.id, identifier, rule_id)

    @app.get("/api/groups/{identifier}/whitelist")
    async def whitelist(identifier: int, request: Request, user: Identity = Depends(identity)):
        return (await request.app.state.management.queries.page(user.id, identifier, "whitelist", size=None))[
            "items"
        ]

    @app.put("/api/groups/{identifier}/whitelist")
    async def save_whitelist(
        identifier: int, body: WhitelistInput, request: Request, user: Identity = Depends(identity)
    ):
        return await request.app.state.management.whitelist(user.id, identifier, body)

    @app.delete("/api/groups/{identifier}/whitelist/{user_id}")
    async def remove_whitelist(
        identifier: int, user_id: int, request: Request, user: Identity = Depends(identity)
    ):
        return await request.app.state.management.whitelist(
            user.id, identifier, WhitelistInput(user_id=user_id), remove=True
        )

    @app.get("/api/groups/{identifier}/violations")
    async def violations(
        identifier: int,
        request: Request,
        user_id: int | None = None,
        after: float = 0,
        before: float = 0,
        page_number: int = Query(1, ge=1),
        size: int = Query(20, ge=1, le=100),
        user: Identity = Depends(identity),
    ):
        return await request.app.state.management.queries.page(
            user.id,
            identifier,
            "violation",
            user_id=user_id,
            after=after,
            before=before,
            page_number=page_number,
            size=size,
        )

    @app.post("/api/groups/{identifier}/violations/{violation_id}/actions")
    async def violation_action(
        identifier: int,
        violation_id: str,
        body: ActionInput,
        request: Request,
        user: Identity = Depends(identity),
    ):
        return await request.app.state.management.violation_action(user.id, identifier, violation_id, body)

    @app.get("/api/admins")
    async def admins(request: Request, user: Identity = Depends(root_identity)):
        return {
            "items": [],
            "superadmin_ids": request.app.state.settings.superadmin_ids,
            "access_mode": "telegram_group_admin",
        }

    @app.put("/api/admins")
    async def save_admin(body: AdminInput, request: Request, user: Identity = Depends(root_identity)):
        return await request.app.state.management.admin(user.id, body)

    @app.get("/api/actions")
    async def action_statuses(
        request: Request,
        identifiers: Annotated[list[str], Query(min_length=1, max_length=50)],
        user: Identity = Depends(identity),
    ):
        return await request.app.state.management.queries.action_statuses(user.id, identifiers)

    @app.get("/api/actions/{identifier}")
    async def action(identifier: str, request: Request, user: Identity = Depends(identity)):
        return await request.app.state.management.queries.action(user.id, identifier)

    @app.get("/api/system")
    async def system(request: Request, user: Identity = Depends(root_identity)):
        return {
            **await request.app.state.management.queries.system(user.id),
            "bot_configured": bool(request.app.state.settings.bot_token),
            "login_configured": request.app.state.settings.login_ready,
            "connected": request.app.state.tg.connected,
            "last_poll": request.app.state.runtime.last_poll,
            "last_error": request.app.state.runtime.last_error,
        }

    @app.post("/api/actions/{identifier}/retry")
    async def retry_action(identifier: str, request: Request, user: Identity = Depends(root_identity)):
        return await request.app.state.management.retry(user.id, identifier)

    @app.post("/api/events/{identifier}/retry")
    async def retry_event(identifier: int, request: Request, user: Identity = Depends(root_identity)):
        return await request.app.state.management.retry(user.id, identifier, event=True)

    @app.post("/api/system/backup")
    async def backup(request: Request, user: Identity = Depends(root_identity)):
        return await request.app.state.management.backup(user.id)
