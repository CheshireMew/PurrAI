"""Versioned, audited management of sample data and durable review receipts."""
import json

from sqlalchemy import func, select

from purrai.advertisement_similarity import BODY_SAMPLE, rank
from purrai.advertisement_store import policy, rescan_pending, samples
from purrai.errors import ManagementError
from purrai.models import AdvertisementPolicy, AdvertisementReview, AdvertisementSample, ArchivedMessage, Meta, Violation, now, uid
from purrai.serialization import row_dict


def check_version(actual, expected):
    if expected is None:
        raise ManagementError(428, "请刷新后再保存")
    if str(actual) != str(expected):
        raise ManagementError(409, "记录已修改，请刷新后核对；草稿尚未保存")


class AdvertisementAdministration:
    def __init__(self, context):
        self.context = context

    async def page(self, actor, group_id, *, page_number=1, query=""):
        await self.context.access.authorize(actor, group_id)
        async with self.context.db.sessions() as session:
            cfg, version = await policy(session, group_id)
            conditions = [AdvertisementSample.group_id == group_id]
            if query:
                conditions.append(AdvertisementSample.text.contains(query, autoescape=True))
            rows = (await session.scalars(select(AdvertisementSample).where(*conditions)
                .order_by(AdvertisementSample.created_at.desc(), AdvertisementSample.id)
                .offset((page_number - 1) * 20).limit(20))).all()
            counts = (await session.execute(select(AdvertisementReview.state, func.count()).where(
                AdvertisementReview.group_id == group_id).group_by(AdvertisementReview.state))).all()
            items = []
            for row in rows:
                corrected = bool(row.violation_id and await session.get(Meta, "violation-correction:" + row.violation_id))
                items.append({**row_dict(row), "corrected": corrected, "usable": row.enabled and row.scope == BODY_SAMPLE and not corrected})
            return {"settings": cfg.model_dump(), "version": version, "items": items,
                    "total": await session.scalar(select(func.count()).select_from(AdvertisementSample).where(*conditions)),
                    "reviews": dict(counts), "model_ready": self.context.settings.chat_ready}

    async def configure(self, actor, group_id, body, expected, request_id):
        await self.context.access.authorize(actor, group_id, write=True)
        intent = {"operation": "advertisement_settings", "group_id": group_id, "body": body.model_dump(), "version": expected}
        async with self.context.access.group_transaction(group_id) as session:
            previous = await self.context.previous(session, actor, request_id, intent)
            if previous is not None:
                return previous
            _, version = await policy(session, group_id)
            check_version(version, expected)
            row = await session.get(AdvertisementPolicy, group_id)
            if row is None:
                row = AdvertisementPolicy(group_id=group_id)
                session.add(row)
            row.config, row.version = body.model_dump(), version + 1
            await rescan_pending(session, group_id)
            return await self.context.record(session, actor, group_id, "advertisement_settings", "修改广告审核设置",
                {"settings": row.config, "version": row.version}, request_id, intent)

    async def save(self, actor, group_id, body, request_id, identifier="", expected=None):
        await self.context.access.authorize(actor, group_id, write=True)
        intent = {"operation": "advertisement_sample", "group_id": group_id, "id": identifier,
                  "body": body.model_dump(), "version": expected}
        async with self.context.access.group_transaction(group_id) as session:
            previous = await self.context.previous(session, actor, request_id, intent)
            if previous is not None:
                return previous
            if identifier:
                row = await session.get(AdvertisementSample, identifier)
                if not row or row.group_id != group_id:
                    raise ManagementError(404, "广告样本不存在")
                check_version(row.version, expected)
                if body.enabled and row.violation_id and await session.get(Meta, "violation-correction:" + row.violation_id):
                    raise ManagementError(409, "原判定已被纠正，请独立核实后新增样本")
                row.version += 1
            else:
                row = AdvertisementSample(group_id=group_id, source="manual", source_key="manual:" + uid())
                session.add(row)
            for key, value in body.model_dump().items():
                setattr(row, key, value)
            row.actor_id, row.source, row.updated_at = actor, "manual", now()
            row.review_state, row.review_error = "done", ""
            await session.flush()
            if row.enabled:
                await rescan_pending(session, group_id)
            return await self.context.record(session, actor, group_id, "advertisement_sample", body.reason,
                                              row_dict(row), request_id, intent)

    async def preview(self, actor, group_id, body):
        await self.context.access.authorize(actor, group_id)
        async with self.context.db.sessions() as session:
            cfg, _ = await policy(session, group_id)
            bank = await samples(session, group_id)
        import asyncio
        result = await asyncio.to_thread(rank, body.text, bank, cfg, context_sensitive=body.quoted, scope=body.scope)
        if result:
            result["sample"] = row_dict(next(row for row in bank if row.id == result["sample_id"]))
        return {"enabled": cfg.enabled, "result": result, "action": cfg.sample_action if cfg.enabled and result and result["matched"] else "review"}

    async def history(self, actor, group_id, *, state="", page_number=1):
        await self.context.access.authorize(actor, group_id)
        async with self.context.db.sessions() as session:
            conditions = [AdvertisementReview.group_id == group_id]
            if state:
                conditions.append(AdvertisementReview.state == state)
            rows = (await session.scalars(select(AdvertisementReview).where(*conditions)
                .order_by(AdvertisementReview.created_at.desc(), AdvertisementReview.id)
                .offset((page_number - 1) * 20).limit(20))).all()
            return {"items": [row_dict(row) for row in rows],
                    "total": await session.scalar(select(func.count()).select_from(AdvertisementReview).where(*conditions))}

    async def retry(self, actor, group_id, body, request_id):
        await self.context.access.authorize(actor, group_id, write=True)
        intent = {"operation": "advertisement_retry", "group_id": group_id, "ids": body.ids}
        async with self.context.access.group_transaction(group_id) as session:
            previous = await self.context.previous(session, actor, request_id, intent)
            if previous is not None:
                return previous
            changed = []
            for identifier in body.ids:
                row = await session.get(AdvertisementReview, identifier)
                if row and row.group_id == group_id and row.message.get("_retained"):
                    raise ManagementError(409, "审核原文已到保留期限，不能重新审核历史摘要")
                if row and row.group_id == group_id and row.state in ("failed", "pending"):
                    row.state, row.error, row.available_at, row.attempts = "pending", "", now(), 0
                    changed.append(identifier)
            return await self.context.record(session, actor, group_id, "advertisement_retry", "重试未完成广告审核",
                                              {"ids": changed}, request_id, intent)

    async def import_history(self, actor, group_id, body, request_id=""):
        await self.context.access.authorize(actor, group_id, write=body.apply)
        intent = {"operation": "advertisement_import", "group_id": group_id}
        async with self.context.access.group_transaction(group_id) as session:
            if body.apply:
                previous = await self.context.previous(session, actor, request_id, intent)
                if previous is not None:
                    return previous
            candidates, corrected_ids = {}, set()
            from purrai.shared_policy import effective_rules
            from purrai.reason_policy import matches
            rules = await effective_rules(session, group_id)
            for row in await session.scalars(select(Violation).where(Violation.group_id == group_id, Violation.message_id > 0)):
                if await session.get(Meta, "violation-correction:" + row.id):
                    corrected_ids.add(row.message_id)
                    continue
                if not any(matches(reason, "ai_history", rules) for reason in row.reasons):
                    continue
                candidates[row.message_id] = {"message_id": row.message_id, "text": row.excerpt,
                    "sender_name": row.sender_name, "reason": "；".join(row.reasons), "violation_id": row.id}
            for row in await session.scalars(select(Meta).where(Meta.key.startswith(f"ai_advertising:{group_id}:"))):
                for item in json.loads(row.value).get("messages", []):
                    if item["id"] in corrected_ids:
                        continue
                    candidates.setdefault(item["id"], {"message_id": item["id"], "text": item["excerpt"],
                        "sender_name": "", "reason": "历史模型广告判定", "violation_id": ""})
            imported = []
            for mid, item in candidates.items():
                key = f"history:{group_id}:{mid}"
                if await session.scalar(select(AdvertisementSample.id).where(
                    AdvertisementSample.group_id == group_id, AdvertisementSample.source_key == key)):
                    continue
                archive = await session.scalar(select(ArchivedMessage).where(
                    ArchivedMessage.group_id == group_id, ArchivedMessage.message_id == mid))
                # Never replace recorded evidence by subsequently edited content.
                if archive and not archive.edited_at:
                    item["text"] = archive.text or archive.caption or item["text"]
                    item["sender_name"] = archive.sender_name
                if not item["text"]:
                    continue
                if body.apply:
                    session.add(AdvertisementSample(group_id=group_id, source="history", source_key=key,
                        scope="context", enabled=True, actor_id=actor, review_state="pending", **item))
                imported.append(item)
            result = {"count": len(imported), "items": imported, "applied": body.apply,
                      "scope": "context"}
            if not body.apply:
                return result
            return await self.context.record(session, actor, group_id, "advertisement_import", "导入历史广告判定，待确认正文适用范围",
                                              result, request_id, intent)
