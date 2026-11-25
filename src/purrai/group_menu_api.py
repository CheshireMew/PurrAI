from fastapi import Depends, Request

from purrai.auth import Identity, identity
from purrai.group_menu_admin import GroupMenuAdmin
from purrai.group_menu_schemas import MenuOperation, MenuSave
from purrai.points_api import RequestKey


def install_group_menu_api(app):
    @app.get("/api/groups/{group_id}/menu")
    async def get(group_id: int, request: Request, user: Identity = Depends(identity)):
        return await GroupMenuAdmin(request.app.state.management).get(user.id, group_id)

    @app.put("/api/groups/{group_id}/menu")
    async def save(group_id: int, body: MenuSave, request: Request, request_id: RequestKey,
                   user: Identity = Depends(identity)):
        return await GroupMenuAdmin(request.app.state.management).change(user.id, group_id, body, request_id, save=True)

    @app.post("/api/groups/{group_id}/menu/operate")
    async def operate(group_id: int, body: MenuOperation, request: Request, request_id: RequestKey,
                      user: Identity = Depends(identity)):
        return await GroupMenuAdmin(request.app.state.management).change(user.id, group_id, body, request_id)
