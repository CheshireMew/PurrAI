"""Current review policy, independent evidence, and source invalidation."""
import hashlib
import json

from sqlalchemy import exists, select, update

from purrai.advertisement_schemas import AdvertisementSettings
from purrai.models import Action, AdvertisementPolicy, AdvertisementReview, AdvertisementSample, Meta, now
from purrai.sender_profile import biography


def criteria_version(rules):
    from purrai.business_rules import document_for
    return hashlib.sha256(document_for(rules).get("ai_review_instructions", "").encode()).hexdigest()


async def policy(session, group_id):
    row = await session.get(AdvertisementPolicy, group_id)
    return AdvertisementSettings.model_validate(row.config if row else {}), row.version if row else 0


async def latest_reviews(session, group_id, message_ids):
    rows = await session.scalars(select(AdvertisementReview).where(
        AdvertisementReview.group_id == group_id, AdvertisementReview.message_id.in_(message_ids),
        AdvertisementReview.state != "superseded").order_by(AdvertisementReview.event_at.desc(),
            AdvertisementReview.update_id.desc(), AdvertisementReview.created_at.desc(), AdvertisementReview.id.desc()))
    result = {}
    for row in rows:
        result.setdefault(row.message_id, row)
    return result


def revision(message):
    # References and sender identity are evidence, not merely the message body.
    evidence = {key: message.get(key) for key in (
        "text", "caption", "from", "sender_chat", "entities", "caption_entities", "reply_to_message",
        "quote", "forward_origin", "forward_from", "forward_from_chat", "external_reply",
        "edit_date", "date", "photo", "video", "document", "reply_markup", "via_bot",
        "guest_bot_caller_user", "guest_bot_caller_chat", "is_automatic_forward")}
    return hashlib.sha256(json.dumps(evidence, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


async def samples(session, group_id):
    return (await session.scalars(select(AdvertisementSample).where(
        AdvertisementSample.group_id == group_id, AdvertisementSample.enabled.is_(True),
        ~exists().where(Meta.key == "violation-correction:" + AdvertisementSample.violation_id),
    ).order_by(AdvertisementSample.created_at.desc()))).all()


async def rescan_pending(session, group_id):
    await session.execute(update(AdvertisementReview).where(AdvertisementReview.group_id == group_id,
        AdvertisementReview.state == "pending", AdvertisementReview.local_after > 0).values(local_after=0))


async def wake_chat_reward(session, review):
    await session.execute(update(Action).where(
        Action.dedupe_key == f"points:{review.group_id}:{review.message_id}", Action.state == "pending",
        Action.payload["operation"].as_string() == "chat").values(available_at=now()))


async def remember(session, review, *, scope, reason, violation_id=""):
    """Only a model's own verdict creates a sample; local matches never call this."""
    key = "review:" + review.id
    existing = await session.scalar(select(AdvertisementSample).where(
        AdvertisementSample.group_id == review.group_id, AdvertisementSample.source_key == key))
    if existing:
        return existing
    from purrai.message_identity import profile_name, message_sender
    message = review.message
    row = AdvertisementSample(group_id=review.group_id, text=biography(message) if scope == "bio" else message.get("text") or message.get("caption") or "",
        sender_name=profile_name(message_sender(message)), scope=scope, source="model", source_key=key,
        message_id=review.message_id, violation_id=violation_id, reason=reason)
    session.add(row)
    await session.flush()
    if scope in ("text", "bio"):
        await rescan_pending(session, review.group_id)
    return row


async def current(session, review):
    if review.message.get("_profile_event"):
        from purrai.models import Member
        member = await session.get(Member, (review.group_id, review.user_id))
        return bool(member and member.present and member.generation == review.message["_profile_event"])
    if review.message_id <= 0:
        return False  # Group upgrades retain old messages under negative historical IDs.
    later = await session.scalar(select(AdvertisementReview.id).where(
        AdvertisementReview.group_id == review.group_id, AdvertisementReview.message_id == review.message_id,
        ((AdvertisementReview.event_at > review.event_at)
         | ((AdvertisementReview.event_at == review.event_at) & (AdvertisementReview.update_id > review.update_id)))))
    if later:
        return False
    from purrai.models import ArchivedMessage, MessageRevision
    archive = await session.scalar(select(ArchivedMessage).where(
        ArchivedMessage.group_id == review.group_id, ArchivedMessage.message_id == review.message_id))
    if archive:
        latest = await session.scalar(select(MessageRevision).where(MessageRevision.archive_id == archive.id)
            .order_by(MessageRevision.event_at.desc(), MessageRevision.is_edit.desc(),
                      MessageRevision.update_id.desc(), MessageRevision.id.desc()).limit(1))
        from purrai.archive import snapshot
        review_version = (review.event_at, bool(review.message.get("edit_date")), review.update_id)
        if (latest and (latest.event_at, latest.is_edit, latest.update_id) >= review_version
                and revision(latest.payload) != revision(snapshot(review.message))):
            return False
    return True


async def evidence_applies(session, action, group):
    evidence = action.payload.get("advertisement_evidence") or {}
    if not group or not group.approved or not group.enabled:
        return False
    if evidence.get("bio_digest"):
        from purrai.sender_profile import evidence_current
        if not await evidence_current(session, action, group, evidence["bio_digest"]):
            return False
    # The network recheck releases the writer; read all policy and review state
    # afterwards so a configuration edit during that request takes effect.
    review = await session.get(AdvertisementReview, evidence.get("review_id", ""), populate_existing=True)
    if not group or not group.approved or not group.enabled or not review or review.state not in ("advertisement", "matched"):
        return False
    cfg, version = await policy(session, group.id)
    if not cfg.enabled or version != evidence.get("policy_version") or not await current(session, review):
        return False
    if "criteria_version" in evidence:
        from purrai.shared_policy import effective_rules
        if evidence["criteria_version"] != criteria_version(await effective_rules(session, group.id)):
            return False
    if await session.get(Meta, "violation-correction:" + action.payload.get("violation_id", "")):
        return False
    for item in evidence.get("context", []):
        previous = await session.get(AdvertisementReview, item["id"], populate_existing=True)
        if not previous or previous.revision != item["revision"] or not await current(session, previous):
            return False
    if review.state == "matched":
        sample = await session.get(AdvertisementSample, evidence.get("sample_id", ""), populate_existing=True)
        if (not cfg.similarity_enabled or not sample or not sample.enabled or sample.scope != evidence.get("sample_scope", "text")
                or sample.version != evidence.get("sample_version")
                or await session.get(Meta, "violation-correction:" + sample.violation_id)):
            return False
    return review.revision == evidence.get("revision") and now() - review.event_at < 47 * 3600
