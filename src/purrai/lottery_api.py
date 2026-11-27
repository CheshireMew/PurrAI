"""Lottery management endpoints; participation belongs to Telegram identities."""
from fastapi import Depends, Query, Request

from purrai import response_models as responses
from purrai.auth import Identity, identity
from purrai.lottery_schemas import LotteryOperation, LotterySave
from purrai.api_dependencies import RequestKey


def install_lottery_api(app):
    @app.get("/api/groups/{group_id}/lotteries", response_model=responses.Page[responses.Lottery], response_model_exclude_unset=True)
    async def page(group_id: int, request: Request, user: Identity = Depends(identity),
                   page_number: int = Query(1, ge=1)):
        return await request.app.state.management.lotteries.page(user.id, group_id, page_number)

    @app.get("/api/groups/{group_id}/lotteries/{identifier}", response_model=responses.LotteryDetail, response_model_exclude_unset=True)
    async def detail(group_id: int, identifier: str, request: Request, user: Identity = Depends(identity),
                     page_number: int = Query(1, ge=1),
                     entry_state: str = Query("", pattern="^(|joined|won|lost|refunded|refund_pending)$")):
        return await request.app.state.management.lotteries.get(user.id, group_id, identifier,
                                                                   page_number, entry_state)

    @app.post("/api/groups/{group_id}/lotteries", response_model=responses.Lottery, response_model_exclude_unset=True)
    async def create(group_id: int, body: LotterySave, request: Request, request_id: RequestKey,
                     user: Identity = Depends(identity)):
        return await request.app.state.management.lotteries.save(user.id, group_id, body, request_id)

    @app.put("/api/groups/{group_id}/lotteries/{identifier}", response_model=responses.Lottery, response_model_exclude_unset=True)
    async def save(group_id: int, identifier: str, body: LotterySave, request: Request, request_id: RequestKey,
                   user: Identity = Depends(identity)):
        return await request.app.state.management.lotteries.save(user.id, group_id, body, request_id, identifier)

    @app.post("/api/groups/{group_id}/lotteries/{identifier}/operate", response_model=responses.Lottery, response_model_exclude_unset=True)
    async def operate(group_id: int, identifier: str, body: LotteryOperation, request: Request, request_id: RequestKey,
                      user: Identity = Depends(identity)):
        return await request.app.state.management.lotteries.operate(user.id, group_id, identifier, body, request_id)
