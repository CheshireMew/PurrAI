from typing import Annotated

from fastapi import Depends, Header, Query, Request

from purrai.auth import Identity, identity
from purrai.points import PointsLedger
from purrai.points_schemas import PointsAdjustment, PointsCreate, PointsIdentity, PointsImportInput, PointsSettingsInput

RequestKey = Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=80)]


def install_points_api(app):
    async def ledger(request, user, group_id, write=False):
        await request.app.state.management.authorize(user.id, group_id, write=write)
        return PointsLedger(request.app.state.db)

    @app.get("/api/groups/{group_id}/points/settings")
    async def settings(group_id: int, request: Request, user: Identity = Depends(identity)):
        from purrai.points_policy import policy_document
        store = await ledger(request, user, group_id)
        async with store.db.sessions() as session:
            return await policy_document(session, group_id)

    @app.put("/api/groups/{group_id}/points/settings")
    async def save_settings(group_id: int, body: PointsSettingsInput, request: Request, request_id: RequestKey,
                            user: Identity = Depends(identity)):
        from purrai.models import PointsPolicy
        from purrai.errors import ManagementError
        from purrai.admin_context import AdminContext
        from purrai.points_policy import policy_document
        store = await ledger(request, user, group_id, True)
        management = request.app.state.management
        context = AdminContext(store.db, management.tg, management.settings, management.access)
        intent = {"group_id": group_id, "operation": "points_policy", **body.model_dump()}
        async with store.db.transaction() as session:
            previous = await context.previous(session, user.id, request_id, intent)
            if previous is not None:
                return previous
            row = await session.get(PointsPolicy, group_id)
            if body.version != (row.version if row else 0):
                raise ManagementError(409, "积分设置已更新，请刷新后再保存")
            if row is None:
                row = PointsPolicy(group_id=group_id, version=0)
                session.add(row)
            row.config, row.version = body.config.model_dump(), row.version + 1
            await session.flush()
            return await context.record(session, user.id, group_id, "points_policy", "保存积分设置",
                await policy_document(session, group_id), request_id, intent)

    @app.get("/api/groups/{group_id}/points")
    async def page(group_id: int, request: Request, user: Identity = Depends(identity),
                   query: str = Query("", max_length=250), pending: bool = False, page_number: int = Query(1, ge=1)):
        return await (await ledger(request, user, group_id)).page(group_id, query=query, pending=pending, page_number=page_number)

    @app.get("/api/groups/{group_id}/points/{account_id}/history")
    async def history(group_id: int, account_id: str, request: Request, user: Identity = Depends(identity),
                      page_number: int = Query(1, ge=1)):
        return await (await ledger(request, user, group_id)).history(group_id, account_id, page_number)

    @app.post("/api/groups/{group_id}/points/import/preview")
    async def preview(group_id: int, body: PointsImportInput, request: Request, user: Identity = Depends(identity)):
        return await (await ledger(request, user, group_id, True)).import_data(group_id, body, user.id, preview=True)

    @app.post("/api/groups/{group_id}/points/import")
    async def import_points(group_id: int, body: PointsImportInput, request: Request, user: Identity = Depends(identity)):
        return await (await ledger(request, user, group_id, True)).import_data(group_id, body, user.id)

    @app.post("/api/groups/{group_id}/points")
    async def create(group_id: int, body: PointsCreate, request: Request, request_id: RequestKey,
                     user: Identity = Depends(identity)):
        return await (await ledger(request, user, group_id, True)).create(group_id, body, user.id, request_id)

    @app.post("/api/groups/{group_id}/points/{account_id}/adjust")
    async def adjust(group_id: int, account_id: str, body: PointsAdjustment, request: Request, request_id: RequestKey,
                     user: Identity = Depends(identity)):
        return await (await ledger(request, user, group_id, True)).change(group_id, account_id, body, user.id, request_id)

    @app.put("/api/groups/{group_id}/points/{account_id}/identity")
    async def bind(group_id: int, account_id: str, body: PointsIdentity, request: Request, request_id: RequestKey,
                   user: Identity = Depends(identity)):
        return await (await ledger(request, user, group_id, True)).change(group_id, account_id, body, user.id, request_id, identity=True)
