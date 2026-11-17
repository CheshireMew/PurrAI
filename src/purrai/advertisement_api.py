"""Public management API, shared with the web console and external agents."""
from typing import Annotated

from fastapi import Depends, Header, Query, Request

from purrai.auth import Identity, identity
from purrai.advertisement_schemas import AdvertisementImport, AdvertisementPreview, AdvertisementRetry, AdvertisementSampleInput, AdvertisementSettings


def install_advertisement_api(app):
    @app.get("/api/groups/{group_id}/advertisement-samples")
    async def page(group_id: int, request: Request, user: Identity = Depends(identity),
                   page_number: int = Query(1, ge=1), query: str = Query("", max_length=250)):
        return await request.app.state.management.advertisements.page(user.id, group_id, page_number=page_number, query=query)

    @app.put("/api/groups/{group_id}/advertisement-settings")
    async def settings(group_id: int, body: AdvertisementSettings, request: Request,
                       request_id: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=80)],
                       version: Annotated[str, Header(alias="If-Match")], user: Identity = Depends(identity)):
        return await request.app.state.management.advertisements.configure(user.id, group_id, body, version, request_id)

    @app.post("/api/groups/{group_id}/advertisement-samples")
    async def create(group_id: int, body: AdvertisementSampleInput, request: Request,
                     request_id: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=80)],
                     user: Identity = Depends(identity)):
        return await request.app.state.management.advertisements.save(user.id, group_id, body, request_id)

    @app.put("/api/groups/{group_id}/advertisement-samples/{identifier}")
    async def edit(group_id: int, identifier: str, body: AdvertisementSampleInput, request: Request,
                   request_id: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=80)],
                   version: Annotated[str, Header(alias="If-Match")], user: Identity = Depends(identity)):
        return await request.app.state.management.advertisements.save(user.id, group_id, body, request_id, identifier, version)

    @app.post("/api/groups/{group_id}/advertisement-samples/preview")
    async def preview(group_id: int, body: AdvertisementPreview, request: Request, user: Identity = Depends(identity)):
        return await request.app.state.management.advertisements.preview(user.id, group_id, body)

    @app.post("/api/groups/{group_id}/advertisement-samples/import-history")
    async def importing(group_id: int, body: AdvertisementImport, request: Request,
                        request_id: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=80)],
                        user: Identity = Depends(identity)):
        return await request.app.state.management.advertisements.import_history(user.id, group_id, body, request_id)

    @app.get("/api/groups/{group_id}/advertisement-reviews")
    async def reviews(group_id: int, request: Request, user: Identity = Depends(identity),
                      page_number: int = Query(1, ge=1), state: str = Query("", max_length=30)):
        return await request.app.state.management.advertisements.history(user.id, group_id, state=state, page_number=page_number)

    @app.post("/api/groups/{group_id}/advertisement-reviews/retry")
    async def retry(group_id: int, body: AdvertisementRetry, request: Request,
                    request_id: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=80)],
                    user: Identity = Depends(identity)):
        return await request.app.state.management.advertisements.retry(user.id, group_id, body, request_id)
