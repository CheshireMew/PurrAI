from typing import Annotated

from fastapi import Depends, Header, Query, Request, Response
from fastapi.responses import StreamingResponse

from purrai.admin_catalog import admin_catalog
from purrai.auth import Identity, identity, root_identity
from purrai.business_schemas import BusinessPolicyInput, BusinessPreviewInput
from purrai.auto_reply_schemas import AutoReplyInput, AutoReplyPreview
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
    RuleBatchInput,
    SettingsPatch,
    SharedPolicyPatch,
    WhitelistInput,
    ViolationCorrectionInput,
    ReleasePendingInput,
    ReleaseSelectionInput,
    DeliveryResolutionInput,
    ArchiveQuotaInput,
    ArchivePurgeInput,
    BackupPlanInput,
    BackupExerciseInput,
    ArchivePreviewInput,
    EmergencyInput,
)


def install_api(app):
    @app.get("/api/groups/{identifier}/auto-replies")
    async def auto_replies(identifier: int, request: Request, user: Identity = Depends(identity),
                           query: str = Query("", max_length=250), page_number: int = Query(1, ge=1)):
        return await request.app.state.management.auto_replies.page(user.id, identifier, query=query, page_number=page_number)

    @app.post("/api/groups/{identifier}/auto-replies/preview")
    async def preview_auto_reply(identifier: int, body: AutoReplyPreview, request: Request, user: Identity = Depends(identity)):
        return await request.app.state.management.auto_replies.preview(user.id, identifier, body)

    @app.post("/api/groups/{identifier}/auto-replies")
    async def create_auto_reply(identifier: int, body: AutoReplyInput, request: Request,
                                request_id: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=80)],
                                user: Identity = Depends(identity)):
        return await request.app.state.management.auto_replies.save(user.id, identifier, body, request_id=request_id)

    @app.put("/api/groups/{identifier}/auto-replies/{reply_id}")
    async def update_auto_reply(identifier: int, reply_id: str, body: AutoReplyInput, request: Request,
                                request_id: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=80)],
                                version: Annotated[str, Header(alias="If-Match")], user: Identity = Depends(identity)):
        return await request.app.state.management.auto_replies.save(user.id, identifier, body, reply_id, request_id, version)

    @app.get("/api/settings/business-policy")
    async def business_policy(request: Request, user: Identity = Depends(root_identity)):
        return await request.app.state.management.business_rules.read(user.id)

    @app.put("/api/settings/business-policy")
    async def save_business_policy(body: BusinessPolicyInput, request: Request,
                                   version: Annotated[str, Header(alias="If-Match")],
                                   user: Identity = Depends(root_identity)):
        return await request.app.state.management.business_rules.save(user.id, body, version)

    @app.post("/api/settings/business-policy/preview")
    async def preview_business_policy(body: BusinessPreviewInput, request: Request, user: Identity = Depends(root_identity)):
        return await request.app.state.management.business_rules.preview(user.id, body)

    @app.get("/api/groups/{identifier}/emergency")
    async def emergency(identifier: int, request: Request, user: Identity = Depends(identity)):
        return await request.app.state.management.protection.status(user.id, identifier)

    @app.post("/api/groups/{identifier}/emergency")
    async def change_emergency(identifier: int, body: EmergencyInput, request: Request, user: Identity = Depends(identity)):
        return await request.app.state.management.protection.change(user.id, identifier, body)

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
            "group_ids": None,
        }

    @app.get("/api/catalog")
    async def catalog(user: Identity = Depends(identity)):
        return admin_catalog()

    @app.get("/api/groups")
    async def groups(request: Request, response: Response, user: Identity = Depends(identity)):
        management = request.app.state.management
        rows = await management.groups(user.id)
        if management.access.discovery_status(user.id)["partial"]:
            response.headers["X-PurrAI-Groups-Partial"] = "true"
        return rows

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

    @app.get("/api/groups/{identifier}/settings/inheritance")
    async def inherited_settings(identifier: int, request: Request, user: Identity = Depends(identity)):
        return await request.app.state.management.configuration.inheritance(user.id, identifier)

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

    @app.get("/api/groups/{identifier}/chat/diagnostics")
    async def chat_diagnostics(identifier: int, request: Request, topic_id: int | None = None, state: str = "",
                               page_number: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100),
                               user: Identity = Depends(identity)):
        return await request.app.state.management.chat_admin.diagnostics(user.id, identifier, topic_id=topic_id,
            state=state, page_number=page_number, size=size)

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
        export_id, stream = await request.app.state.management.archive_management.stream(user.id, identifier, filters)
        return StreamingResponse(
            stream,
            media_type="application/x-ndjson",
            headers={
                "Content-Disposition": f'attachment; filename="purrai-{identifier}-messages.jsonl"',
                "Cache-Control": "no-store",
                "X-PurrAI-Export-ID": export_id,
            },
        )

    @app.get("/api/groups/{identifier}/archive/capacity")
    async def archive_capacity(identifier: int, request: Request, user: Identity = Depends(identity)):
        return await request.app.state.management.archive_management.capacity(user.id, identifier)

    @app.patch("/api/groups/{identifier}/archive/capacity")
    async def archive_quota(identifier: int, body: ArchiveQuotaInput, request: Request, user: Identity = Depends(root_identity)):
        return await request.app.state.management.archive_management.quota(user.id, identifier, body)

    @app.get("/api/groups/{identifier}/archive/exports")
    async def archive_exports(identifier: int, request: Request, user: Identity = Depends(identity)):
        return await request.app.state.management.archive_management.exports(user.id, identifier)

    @app.post("/api/groups/{identifier}/archive/purge")
    async def archive_purge(identifier: int, body: ArchivePurgeInput, request: Request, user: Identity = Depends(identity)):
        return await request.app.state.management.archive_management.purge(user.id, identifier, body)

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

    @app.get("/api/groups/{identifier}/members/{user_id}")
    async def member_detail(identifier: int, user_id: int, request: Request, user: Identity = Depends(identity)):
        return await request.app.state.management.member_queries.detail(user.id, identifier, user_id)

    @app.post("/api/groups/{identifier}/members/{user_id}/actions")
    async def member_action(identifier: int, user_id: int, body: ActionInput, request: Request,
                            user: Identity = Depends(identity)):
        return await request.app.state.management.member_action(user.id, identifier, user_id, body.action,
            body.request_id, body.reason, body.duration_seconds)

    @app.post("/api/groups/{identifier}/release-pending")
    async def release_pending(
        identifier: int, body: ReleasePendingInput, request: Request, user: Identity = Depends(identity)
    ):
        return await request.app.state.management.release_pending(user.id, identifier, body)

    @app.post("/api/groups/{identifier}/release-pending/preview")
    async def preview_release(identifier: int, body: ReleaseSelectionInput, request: Request,
                              user: Identity = Depends(identity)):
        return await request.app.state.management.members.release_preview(user.id, identifier, body)

    @app.get("/api/groups/{identifier}/actions")
    async def group_actions(
        identifier: int,
        request: Request,
        state: str = "",
        user_id: int | None = None,
        actor_id: int | None = None,
        kind: str = "",
        query: str = Query("", max_length=250),
        after: float = 0,
        before: float = 0,
        manual_only: bool = True,
        system_only: bool = False,
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
            manual_only=manual_only, system_only=system_only, user_id=user_id, actor_id=actor_id,
            kind=kind, query=query, after=after, before=before,
        )

    @app.get("/api/groups/{identifier}/rules")
    async def rules(identifier: int, request: Request, user: Identity = Depends(identity)):
        return (await request.app.state.management.queries.page(user.id, identifier, "rule", size=None))[
            "items"
        ]

    @app.get("/api/groups/{identifier}/rules/query")
    async def query_rules(identifier: int, request: Request, query: str = Query("", max_length=250),
                          kind: str = "", enabled: bool | None = None, source: str = "",
                          page_number: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100),
                          user: Identity = Depends(identity)):
        return await request.app.state.management.rule_queries.page(user.id, identifier, query=query,
            kind=kind, enabled=enabled, source=source, page_number=page_number, size=size)

    @app.post("/api/groups/{identifier}/rules/batch")
    async def batch_rules(identifier: int, body: RuleBatchInput, request: Request, user: Identity = Depends(identity)):
        return await request.app.state.management.rule_queries.batch(user.id, identifier, body)

    @app.post("/api/groups/{identifier}/rules")
    async def add_rule(
        identifier: int, body: RuleInput, request: Request,
        request_id: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=80)],
        user: Identity = Depends(identity)
    ):
        return await request.app.state.management.rule(user.id, identifier, body, request_id=request_id)

    @app.put("/api/groups/{identifier}/rules/{rule_id}")
    async def edit_rule(
        identifier: int, rule_id: str, body: RuleInput, request: Request,
        request_id: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=80)],
        version: Annotated[str, Header(alias="If-Match")], user: Identity = Depends(identity)
    ):
        return await request.app.state.management.rule(user.id, identifier, body, rule_id, request_id, version)

    @app.post("/api/groups/{identifier}/rules/preview")
    async def preview(
        identifier: int, body: PreviewInput, request: Request, user: Identity = Depends(identity)
    ):
        return await request.app.state.management.preview(user.id, identifier, body)

    @app.post("/api/groups/{identifier}/rules/preview-archive")
    async def preview_archive(identifier: int, body: ArchivePreviewInput, request: Request, user: Identity = Depends(identity)):
        return await request.app.state.management.archive_preview.run(user.id, identifier, body)

    @app.post("/api/groups/{identifier}/rules/{rule_id}/inherit")
    async def inherit_rule(identifier: int, rule_id: str, request: Request,
                           request_id: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=80)],
                           version: Annotated[str, Header(alias="If-Match")], user: Identity = Depends(identity)):
        return await request.app.state.management.rules.inherit(user.id, identifier, rule_id, version, request_id)

    @app.get("/api/groups/{identifier}/whitelist")
    async def whitelist(identifier: int, request: Request, user: Identity = Depends(identity)):
        return (await request.app.state.management.queries.page(user.id, identifier, "whitelist", size=None))[
            "items"
        ]

    @app.put("/api/groups/{identifier}/whitelist")
    async def save_whitelist(
        identifier: int, body: WhitelistInput, request: Request,
        request_id: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=80)],
        version: Annotated[str, Header(alias="If-Match")], user: Identity = Depends(identity)
    ):
        return await request.app.state.management.whitelist(user.id, identifier, body, request_id, expected_version=version)

    @app.delete("/api/groups/{identifier}/whitelist/{user_id}")
    async def remove_whitelist(
        identifier: int, user_id: int, request: Request,
        request_id: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=80)],
        version: Annotated[str, Header(alias="If-Match")], user: Identity = Depends(identity)
    ):
        return await request.app.state.management.whitelist(
            user.id, identifier, WhitelistInput(user_id=user_id), request_id, remove=True, expected_version=version
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

    @app.get("/api/groups/{identifier}/violations/{violation_id}/correction")
    async def correction_preview(identifier: int, violation_id: str, request: Request,
                                 user: Identity = Depends(identity)):
        return await request.app.state.management.corrections.preview(user.id, identifier, violation_id)

    @app.post("/api/groups/{identifier}/violations/{violation_id}/correction")
    async def correct_violation(identifier: int, violation_id: str, body: ViolationCorrectionInput,
                                request: Request, user: Identity = Depends(identity)):
        return await request.app.state.management.corrections.apply(user.id, identifier, violation_id, body)

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
            "last_poll": request.app.state.runtime.receiver.last_poll,
            **request.app.state.runtime.status(),
        }

    @app.get("/api/system/failures")
    async def failed_page(request: Request, event: bool = False, page_number: int = Query(1, ge=1),
                           size: int = Query(20, ge=1, le=100), user: Identity = Depends(root_identity)):
        return await request.app.state.management.queries.failed_page(user.id, event=event, page_number=page_number, size=size)

    @app.post("/api/actions/{identifier}/retry")
    async def retry_action(identifier: str, request: Request, user: Identity = Depends(root_identity)):
        return await request.app.state.management.retry(user.id, identifier)

    @app.post("/api/actions/{identifier}/resolve")
    async def resolve_delivery(identifier: str, body: DeliveryResolutionInput, request: Request,
                                user: Identity = Depends(root_identity)):
        return await request.app.state.management.system_admin.resolve_delivery(user.id, identifier, body)

    @app.post("/api/events/{identifier}/retry")
    async def retry_event(identifier: int, request: Request, user: Identity = Depends(root_identity)):
        return await request.app.state.management.retry(user.id, identifier, event=True)

    @app.post("/api/system/backup")
    async def backup(request: Request, user: Identity = Depends(root_identity)):
        return await request.app.state.management.backup(user.id)

    @app.get("/api/system/backups")
    async def backup_list(request: Request, user: Identity = Depends(root_identity)):
        return await request.app.state.management.backups.status(user.id)

    @app.patch("/api/system/backups/plan")
    async def backup_plan(body: BackupPlanInput, request: Request, user: Identity = Depends(root_identity)):
        return await request.app.state.management.backups.plan(user.id, body)

    @app.post("/api/system/backups/exercise")
    async def backup_exercise(body: BackupExerciseInput, request: Request, user: Identity = Depends(root_identity)):
        return await request.app.state.management.backups.exercise(user.id, body)

    @app.get("/api/system/alerts")
    async def runtime_alerts(request: Request, user: Identity = Depends(root_identity)):
        return await request.app.state.runtime.alerts.status()
