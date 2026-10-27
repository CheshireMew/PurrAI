"""Chat history storage shared by observation, context and delivery receipts."""

from sqlalchemy import select, update

from purrai.models import ChatMessage


async def recent_messages(session, group_id, topic_id):
    return (await session.scalars(select(ChatMessage).where(
        ChatMessage.group_id == group_id, ChatMessage.topic_id == topic_id,
    ).order_by(ChatMessage.sent_at.desc(), ChatMessage.message_id.desc()).limit(100))).all()


async def invalidate_message(session, group_id, message_id):
    await session.execute(update(ChatMessage).where(
        ChatMessage.group_id == group_id, ChatMessage.message_id == message_id,
    ).values(eligible=False))
