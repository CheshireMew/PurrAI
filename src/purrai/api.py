from typing import Annotated

from fastapi import Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select

from purrai.auth import Identity, authorize_group, group_ids, identity, root_identity
from purrai.moderation_records import violation_actions
from purrai.models import (
    Action,
    Group,
    Inbox,
    Meta,
    Rule,
    Verification,
    Violation,
    Whitelist,
    now,
)
from purrai.schemas import (
    ActionInput,
    AdminInput,
    ArchiveFilter,
    ArchiveQuery,
    EmojiPackChange,
    GroupAccessInput,
    PreviewInput,
    RuleInput,
    SettingsPatch,
    WhitelistInput,
)
from purrai.service import ACTIVE, row_dict


def install_api(app):
    async def get_group(request, user, identifier, write=False):
        await authorize_group(request, user, identifier, write)
        async with request.app.state.db.sessions() as session:
            group = await session.get(Group, identifier)
            if group is None:
                raise HTTPException(404, "群不存在，请先将机器人加入群")
            return group

    @app.get("/api/me")
    async def me(request: Request, user: Identity = Depends(identity)):
        return {
            "id": user.id,
            "root": user.root,
            "csrf": user.csrf,
            "group_ids": await group_ids(request, user),
        }

    @app.get("/api/groups")
    async def groups(request: Request, user: Identity = Depends(identity)):
        identifiers = await group_ids(request, user)
        query = select(Group).order_by(Group.title)
        if identifiers is not None:
            query = query.where(Group.id.in_(identifiers))
        async with request.app.state.db.sessions() as session:
            return [row_dict(group) for group in (await session.scalars(query)).all()]

    @app.post("/api/groups/{identifier}/access")
    async def group_access(identifier: int, body: GroupAccessInput, request: Request, user: Identity = Depends(root_identity)):
        return await request.app.state.management.group_access(user.id, identifier, body.approved)

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
        identifiers = await group_ids(request, user)
        async with request.app.state.db.sessions() as session:

            async def count(model, *conditions):
                query = select(func.count()).select_from(model).where(*conditions)
                if identifiers is not None:
                    query = query.where((model.id if model is Group else model.group_id).in_(identifiers))
                return await session.scalar(query)

            return {
                "groups": await count(Group),
                "active_groups": await count(Group, Group.enabled.is_(True)),
                "pending": await count(Verification, Verification.state.in_(ACTIVE)),
                "passed_today": await count(
                    Verification, Verification.state == "passed", Verification.updated_at >= now() - 86400
                ),
                "failed_today": await count(
                    Verification, Verification.state == "failed", Verification.updated_at >= now() - 86400
                ),
                "violations_today": await count(Violation, Violation.created_at >= now() - 86400),
                "failed_actions": await count(Action, Action.state == "failed"),
            }

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
        from purrai.archive import export_jsonl

        await get_group(request, user, identifier)
        return StreamingResponse(
            export_jsonl(request.app.state.db, identifier, filters),
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

    async def page(session, model, filters, page_number, size):
        total = await session.scalar(select(func.count()).select_from(model).where(*filters))
        rows = (
            await session.scalars(
                select(model)
                .where(*filters)
                .order_by(model.created_at.desc())
                .offset((page_number - 1) * size)
                .limit(size)
            )
        ).all()
        return {"items": [row_dict(row) for row in rows], "total": total, "page": page_number, "size": size}

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
        await get_group(request, user, identifier)
        filters = [Verification.group_id == identifier]
        if state:
            filters.append(Verification.state == state)
        if user_id:
            filters.append(Verification.user_id == user_id)
        if after:
            filters.append(Verification.created_at >= after)
        if before:
            filters.append(Verification.created_at <= before)
        async with request.app.state.db.sessions() as session:
            result = await page(session, Verification, filters, page_number, size)
            for item in result["items"]:
                for secret in ("answer_hash", "input_digits", "nonce"):
                    item.pop(secret, None)
            return result

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
        await get_group(request, user, identifier)
        filters = [Action.group_id == identifier, Action.actor_id.is_not(None)]
        if state:
            filters.append(Action.state == state)
        async with request.app.state.db.sessions() as session:
            return await page(session, Action, filters, page_number, size)

    @app.get("/api/groups/{identifier}/rules")
    async def rules(identifier: int, request: Request, user: Identity = Depends(identity)):
        await get_group(request, user, identifier)
        async with request.app.state.db.sessions() as session:
            return [
                row_dict(row)
                for row in (await session.scalars(select(Rule).where(Rule.group_id == identifier))).all()
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

    @app.get("/api/groups/{identifier}/whitelist")
    async def whitelist(identifier: int, request: Request, user: Identity = Depends(identity)):
        await get_group(request, user, identifier)
        async with request.app.state.db.sessions() as session:
            return [
                row_dict(row)
                for row in (
                    await session.scalars(select(Whitelist).where(Whitelist.group_id == identifier))
                ).all()
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
        await get_group(request, user, identifier)
        filters = [Violation.group_id == identifier]
        if user_id:
            filters.append(Violation.user_id == user_id)
        if after:
            filters.append(Violation.created_at >= after)
        if before:
            filters.append(Violation.created_at <= before)
        async with request.app.state.db.sessions() as session:
            result = await page(session, Violation, filters, page_number, size)
            for item in result["items"]:
                item["actions"] = [row_dict(a) for a in await violation_actions(session, identifier, item["id"])]
            return result

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

    @app.get("/api/actions/{identifier}")
    async def action(identifier: str, request: Request, user: Identity = Depends(identity)):
        async with request.app.state.db.sessions() as session:
            row = await session.get(Action, identifier)
            if not row:
                raise HTTPException(404, "操作不存在")
            if row.group_id is None and not user.root:
                raise HTTPException(403, "仅超级管理员可以查看系统操作")
            if row.group_id:
                await authorize_group(request, user, row.group_id)
            return row_dict(row)

    @app.get("/api/system")
    async def system(request: Request, user: Identity = Depends(root_identity)):
        async with request.app.state.db.sessions() as session:
            pending = await session.scalar(
                select(func.count()).select_from(Action).where(Action.state == "pending")
            )
            failed = (
                await session.scalars(
                    select(Action)
                    .where(Action.state == "failed")
                    .order_by(Action.updated_at.desc())
                    .limit(30)
                )
            ).all()
            failed_inbox = (
                await session.scalars(select(Inbox).where(Inbox.state == "failed").limit(30))
            ).all()
            backup = await session.get(Meta, "last_backup")
            archive_status = await session.get(Meta, "archive:paused")
        return {
            "bot_configured": bool(request.app.state.settings.bot_token),
            "login_configured": request.app.state.settings.login_ready,
            "connected": request.app.state.tg.connected,
            "last_poll": request.app.state.runtime.last_poll,
            "last_error": request.app.state.runtime.last_error,
            "pending_actions": pending,
            "failed_actions": [row_dict(row) for row in failed],
            "failed_events": [
                {"id": row.id, "error": row.error, "attempts": row.attempts} for row in failed_inbox
            ],
            "last_backup": backup.value if backup else None,
            "archive_status": archive_status.value if archive_status else "",
            "chat_budget": await request.app.state.runtime.chat.budget.status(None),
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
