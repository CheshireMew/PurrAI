from fastapi import Depends, Query, Request

from purrai import response_models as responses
from purrai.auth import Identity, identity
from purrai.api_dependencies import RequestKey
from purrai.points import PointsLedger
from purrai.points_schemas import PointsAdjustment, PointsCreate, PointsIdentity, PointsImportInput, PointsSettingsInput

def install_points_api(app):
    async def ledger(request, user, group_id, write=False):
        await request.app.state.management.authorize(user.id, group_id, write=write)
        return PointsLedger(request.app.state.db)

    @app.get("/api/groups/{group_id}/points/settings", response_model=responses.PointsPolicy, response_model_exclude_unset=True)
    async def settings(group_id: int, request: Request, user: Identity = Depends(identity)):
        return await request.app.state.management.points.settings(user.id, group_id)

    @app.put("/api/groups/{group_id}/points/settings", response_model=responses.PointsPolicy, response_model_exclude_unset=True)
    async def save_settings(group_id: int, body: PointsSettingsInput, request: Request, request_id: RequestKey,
                            user: Identity = Depends(identity)):
        return await request.app.state.management.points.save_settings(user.id, group_id, body, request_id)

    @app.get("/api/groups/{group_id}/points", response_model=responses.PointsPage, response_model_exclude_unset=True)
    async def page(group_id: int, request: Request, user: Identity = Depends(identity),
                   query: str = Query("", max_length=250), pending: bool = False, page_number: int = Query(1, ge=1)):
        return await request.app.state.management.points.page(user.id, group_id, query=query, pending=pending, page_number=page_number)

    @app.get("/api/groups/{group_id}/points/{account_id}/history", response_model=responses.PointsHistory, response_model_exclude_unset=True)
    async def history(group_id: int, account_id: str, request: Request, user: Identity = Depends(identity),
                      page_number: int = Query(1, ge=1)):
        return await (await ledger(request, user, group_id)).history(group_id, account_id, page_number)

    @app.post("/api/groups/{group_id}/points/import/preview", response_model=responses.ImportReceipt, response_model_exclude_unset=True)
    async def preview(group_id: int, body: PointsImportInput, request: Request, user: Identity = Depends(identity)):
        return await (await ledger(request, user, group_id, True)).import_data(group_id, body, user.id, preview=True)

    @app.post("/api/groups/{group_id}/points/import", response_model=responses.ImportReceipt, response_model_exclude_unset=True)
    async def import_points(group_id: int, body: PointsImportInput, request: Request, user: Identity = Depends(identity)):
        return await (await ledger(request, user, group_id, True)).import_data(group_id, body, user.id)

    @app.post("/api/groups/{group_id}/points", response_model=responses.PointsAccount, response_model_exclude_unset=True)
    async def create(group_id: int, body: PointsCreate, request: Request, request_id: RequestKey,
                     user: Identity = Depends(identity)):
        return await (await ledger(request, user, group_id, True)).create(group_id, body, user.id, request_id)

    @app.post("/api/groups/{group_id}/points/{account_id}/adjust", response_model=responses.PointsAccount, response_model_exclude_unset=True)
    async def adjust(group_id: int, account_id: str, body: PointsAdjustment, request: Request, request_id: RequestKey,
                     user: Identity = Depends(identity)):
        return await (await ledger(request, user, group_id, True)).change(group_id, account_id, body, user.id, request_id)

    @app.put("/api/groups/{group_id}/points/{account_id}/identity", response_model=responses.PointsAccount, response_model_exclude_unset=True)
    async def bind(group_id: int, account_id: str, body: PointsIdentity, request: Request, request_id: RequestKey,
                   user: Identity = Depends(identity)):
        return await (await ledger(request, user, group_id, True)).change(group_id, account_id, body, user.id, request_id, identity=True)
