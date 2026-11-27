"""One model request, budget settlement and decision protocol for all ad reviews."""

import json

from purrai.advertisement_content import prompt
from purrai.chat_budget import ChatBudget
from purrai.chat_model import ChatModel, ChatModelError
from purrai.model_diagnostics import model_request_context


class AdvertisingModelReview:
    def __init__(self, db, settings, model=None):
        self.model = model or ChatModel(settings)
        self.budget = ChatBudget(db, settings)

    async def review(self, group_id, rules, messages, *, attempts, history=False):
        payload = json.dumps({"messages": messages}, ensure_ascii=False, separators=(",", ":"))
        output_limit = min(4096, max(256, len(messages) * 200))
        system = prompt(rules)
        reservation = await self.budget.reserve(
            group_id, 0, system, payload, review=True, max_tokens=output_limit
        )
        identifiers = {item["id"] for item in messages}
        context = {"sample_ids" if history else "review_ids": sorted(identifiers)}
        with model_request_context(group_id=group_id, attempts=attempts, **context):
            result, tokens = await self.model.completion(
                system, payload, max_tokens=output_limit, retry_invalid=True
            )
        await self.budget.settle(reservation, tokens)
        return self.decisions(result, identifiers, history=history)

    @staticmethod
    def decisions(result, identifiers, *, history=False):
        decisions = result.get("decisions") if isinstance(result, dict) else None
        if not isinstance(decisions, list):
            raise ChatModelError(
                "历史样本复核结果无效" if history else "广告审核结果缺少逐条判定", retryable=True
            )
        scopes = ("text", "context") if history else ("text", "bio", "context")
        seen = set()
        for decision in decisions:
            identifier = decision.get("id") if isinstance(decision, dict) else None
            if identifier not in identifiers or identifier in seen:
                raise ChatModelError(
                    "历史样本复核字段无效" if history else "广告审核结果含重复或未知消息", retryable=True
                )
            seen.add(identifier)
            if (
                type(decision.get("advertisement")) is not bool
                or decision.get("scope") not in scopes
                or not isinstance(decision.get("reason"), str)
                or not history
                and not decision["reason"].strip()
            ):
                raise ChatModelError(
                    "历史样本复核字段无效" if history else "广告审核结果字段无效", retryable=True
                )
        return decisions

    async def close(self):
        await self.model.close()
