from fastapi import Depends, Request

from purrai import response_models as responses
from purrai.auth import Identity, identity
from purrai.group_menu_schemas import MenuOperation, MenuSave
from purrai.api_dependencies import RequestKey


def install_group_menu_api(app):
    @app.get("/api/groups/{group_id}/menu", response_model=responses.Menu, response_model_exclude_unset=True)
    async def get(group_id: int, request: Request, user: Identity = Depends(identity)):
        return await request.app.state.management.group_menu.get(user.id, group_id)

    @app.put("/api/groups/{group_id}/menu", response_model=responses.Menu, response_model_exclude_unset=True)
    async def save(group_id: int, body: MenuSave, request: Request, request_id: RequestKey,
                   user: Identity = Depends(identity)):
        return await request.app.state.management.group_menu.change(user.id, group_id, body, request_id, save=True)

    @app.post("/api/groups/{group_id}/menu/operate", response_model=responses.Menu, response_model_exclude_unset=True)
    async def operate(group_id: int, body: MenuOperation, request: Request, request_id: RequestKey,
                      user: Identity = Depends(identity)):
        return await request.app.state.management.group_menu.change(user.id, group_id, body, request_id)
