"""Revalidate advertising evidence and commit the configured moderation outcome."""

from sqlalchemy import select

from purrai.action_queue import enqueue
from purrai.moderation_records import record_violation

from purrai.advertisement_content import context_sensitive
from purrai.advertisement_similarity import rank
from purrai.advertisement_store import current, policy, remember, samples, wake_chat_reward
from purrai.message_identity import profile_name, message_sender, sender_chat
from purrai.message_parts import message_text
from purrai.models import AdvertisementReview, Group, Member, Meta, Violation, Whitelist, now
from purrai.points_trust import exempt_review
from purrai.policy_work import PolicyExecution, policy_work
from purrai.rules import ai_keyword_hints
from purrai.sender_profile import biography, digest as profile_digest
from purrai.shared_policy import effective_rules
from purrai.telegram import is_admin


class AdvertisementDecisions:
    def __init__(self, db, telegram, settings):
        self.db, self.tg, self.settings = db, telegram, settings

    async def valid(self, session, row, *, allow_expired=False):
        group = await session.get(Group, row.group_id)
        cfg, version = await policy(session, row.group_id)
        white = await session.get(Whitelist, (row.group_id, row.user_id))
        valid = bool(
            group
            and group.approved
            and group.enabled
            and cfg.enabled
            and row.user_id not in self.settings.superadmin_ids
            and not (white and white.moderation)
            and (allow_expired or now() - row.event_at < 47 * 3600)
            and await current(session, row)
        )
        violation = await session.scalar(
            select(Violation).where(
                Violation.group_id == row.group_id, Violation.message_id == row.message_id
            )
        )
        if violation and await session.get(Meta, "violation-correction:" + violation.id):
            valid = False
        from purrai.channel_moderation import trusted

        if await trusted(session, row.group_id, row.message):
            valid = False
        return valid, group, cfg, version

    async def apply(self, identifier, decision, policy_version, *, match=None, hints=None, criteria=None):
        async with self.db.transaction() as session:
            row = await session.get(AdvertisementReview, identifier)
            if not row or row.state not in ("pending", "requesting"):
                return False
            if await exempt_review(session, row):
                return True
            gid, user_id = row.group_id, row.user_id
        if decision["advertisement"] and user_id > 0:
            member = await self.tg.call("get_chat_member", chat_id=gid, user_id=user_id)
            if is_admin(member):
                async with self.db.transaction() as session:
                    row = await session.get(AdvertisementReview, identifier)
                    if row.state in ("pending", "requesting"):
                        row.state, row.error = "exempt", "群管理员免审核处罚"
                        await wake_chat_reward(session, row)
                return True
        async with PolicyExecution(self.db) as session:
            row = await session.get(AdvertisementReview, identifier)
            if row.state not in ("pending", "requesting"):
                return False
            if await exempt_review(session, row):
                return True
            valid, group, cfg, version = await self.valid(session, row)
            if not valid:
                row.state, row.error = "cancelled", "消息、权限或审核设置已失效"
                await wake_chat_reward(session, row)
                return True
            body = message_text(row.message)
            name = profile_name(message_sender(row.message))
            rules = await effective_rules(session, gid)
            if biography(row.message) and not group.settings.get("bio_advertising_enabled", True):
                row.state, row.error, row.available_at = "pending", "简介检查已停用，重新审核正文", now()
                row.local_after = 0
                return True
            latest_hints = await policy_work(
                session, ai_keyword_hints, body, rules, name, biography(row.message)
            )
            from purrai.advertisement_store import criteria_version

            if (
                version != policy_version
                or hints is not None
                and hints != latest_hints
                or criteria is not None
                and criteria != criteria_version(rules)
            ):
                row.state, row.error, row.available_at = "pending", "配置已修改，重新审核", now()
                row.local_after = 0
                return True
            evidence = {"review_id": row.id, "revision": row.revision, "policy_version": version}
            scope = decision["scope"]
            if decision["advertisement"] and not match and biography(row.message):
                evidence["bio_digest"] = profile_digest(row.message["_sender_profile"])
            if scope == "context":
                evidence["context"] = [
                    {"id": item["id"], "revision": item["revision"]}
                    for item in row.message.get("_ad_context", [])
                ]
            if criteria is not None:
                evidence["criteria_version"] = criteria
            if match:
                latest = await policy_work(
                    session,
                    rank,
                    body,
                    await samples(session, gid, direct_text=body),
                    cfg,
                    context_sensitive=scope == "text" and context_sensitive(row.message),
                    scope=scope,
                )
                if (
                    not latest
                    or not latest["matched"]
                    or latest["sample_id"] != match["sample_id"]
                    or latest["sample_version"] != match["sample_version"]
                ):
                    return False
                evidence.update(
                    sample_id=match["sample_id"], sample_version=match["sample_version"], sample_scope=scope
                )
            row.state = ("matched" if match else "advertisement") if decision["advertisement"] else "clean"
            row.result = {
                **decision,
                "source": "similarity" if match else "model",
                **(match or {}),
                "policy_version": version,
                **({"criteria_version": criteria} if criteria is not None else {}),
            }
            row.error, row.updated_at = "", now()
            await wake_chat_reward(session, row)
            if not decision["advertisement"]:
                return True
            if sender_chat(row.message):
                from purrai.channel_moderation import queue_ban

                violation, payload = await self.record_and_queue_delete(
                    session,
                    row,
                    evidence,
                    "频道广告审核：" + decision["reason"],
                    "ad-channel-delete:" + row.id,
                    channel=True,
                )
                await queue_ban(session, group, user_id, "ad-channel-ban:" + row.id, payload)
            elif match:
                violation, payload = await self.record_and_queue_delete(
                    session,
                    row,
                    evidence,
                    f"广告样本相似度 {match['score']:.3f}：{match['sample_id']}",
                    "ad-sample-delete:" + row.id,
                    permanent_mute=cfg.sample_action == "permanent_mute",
                )
                if cfg.sample_action == "permanent_mute":
                    member = await session.get(Member, (gid, user_id))
                    if member is None:
                        member = Member(group_id=gid, user_id=user_id, joined_at=0)
                        session.add(member)
                        await session.flush()
                    await enqueue(
                        session,
                        "sample_ad_mute",
                        "ad-sample-mute:" + row.id,
                        gid,
                        user_id,
                        {**payload, "generation": member.generation},
                        priority=1,
                        reason="已确认广告样本匹配，按配置永久禁言",
                    )
            else:
                from purrai.ai_advertising import record_ai_advertisement

                await record_ai_advertisement(
                    session,
                    group,
                    user_id,
                    row.message_id,
                    body,
                    name,
                    protected_ids=self.settings.superadmin_ids,
                    independent_evidence=evidence,
                    sender_bio=biography(row.message),
                    matched_rules=[
                        rule for rule in rules if rule.get("id") in {hint["rule_id"] for hint in latest_hints}
                    ],
                )
                violation = await session.scalar(
                    select(Violation).where(Violation.group_id == gid, Violation.message_id == row.message_id)
                )
            if not match:
                await remember(
                    session,
                    row,
                    scope="context" if scope == "text" and context_sensitive(row.message) else scope,
                    reason=decision["reason"],
                    violation_id=violation.id if violation else "",
                )
            return True

    @staticmethod
    async def record_and_queue_delete(
        session, row, evidence, reason, delete_key, *, channel=False, permanent_mute=False
    ):
        violation, _ = await record_violation(
            session,
            group_id=row.group_id,
            user_id=row.user_id,
            message_id=row.message_id,
            reasons=[reason],
            excerpt=message_text(row.message),
            sender_name=profile_name(message_sender(row.message)),
            sender_chat=channel,
            message_violation=True,
            rule_mute=permanent_mute,
        )
        payload = {
            "message_id": row.message_id,
            "violation_id": violation.id,
            "advertisement_evidence": evidence,
        }
        if channel:
            payload["channel_evidence"] = {"sender_chat_id": row.user_id}
        await enqueue(session, "delete", delete_key, row.group_id, row.user_id, payload, priority=0)
        return violation, payload
