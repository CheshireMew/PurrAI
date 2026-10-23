"""Text and sticker delivery with current moderation checks and durable receipts."""

import json
import logging

from aiogram.exceptions import TelegramBadRequest
from sqlalchemy import select

from purrai.action_queue import enqueue
from purrai.deletion_effects import deletion_result
from purrai.chat_safety import output_reasons
from purrai.chat_retraction import finish_receipt
from purrai.models import ChatTurn, Rule, now
from purrai.schemas import GroupSettings
from purrai.serialization import row_dict
from purrai.stickers import emoji_key

logger = logging.getLogger(__name__)


class ChatDelivery:
    def __init__(self, db, telegram, context, stickers):
        self.db, self.tg, self.context, self.stickers = db, telegram, context, stickers

    async def deliver(self, turn, cfg, original, sender_id, generated):
        async with self.stickers.lock(turn.group_id):
            return await self.send(turn, cfg, original, sender_id, generated)

    async def missing_target(self, turn, error):
        if not any(reason in str(error).lower() for reason in (
            "message to be replied not found", "reply message not found", "replied message not found",
        )):
            return False
        async with self.db.transaction() as session:
            action = await enqueue(
                session, "delete", f"chat-missing-target:{turn.group_id}:{turn.message_id}", turn.group_id,
                payload={"message_id": turn.message_id, "already_absent": True},
                reason="Telegram 确认被回复的消息已不存在；仅记录结果，不执行删除",
            )
            action.state, action.updated_at = "succeeded", now()
            await deletion_result(session, action)
            current = await session.get(ChatTurn, turn.id)
            if current.sent_message_id is None:
                current.state, current.response = "skipped", ""
                current.error = "被回复的消息已不存在，取消本次回复"
            logger.info("聊天目标不存在 turn=%s group=%s state=%s reason=%s", turn.id, turn.group_id, current.state, current.error)
        return True

    async def send(self, turn, cfg, original, sender_id, generated):
        result, fingerprint = generated.result, generated.fingerprint
        selected_sticker, text_message_id = generated.selected_sticker, generated.text_message_id
        async with self.db.transaction() as session:
            current, group, valid = await self.context.valid_turn(session, turn, original, sender_id, generated.context)
            if not valid:
                if current.state not in ("retracting", "retracted", "retraction_failed"):
                    current.state = "skipped"
                    current.error = "发送前，聊天设置、目标消息或引用上下文已失效"
                logger.info("聊天未发送 turn=%s group=%s reason=%s", turn.id, turn.group_id, current.error)
                return True
            if not result.text and not result.sticker:
                if current.sent_message_id is not None:
                    current.state, current.error = "done", "已有文字已发送，本次停止剩余发送"
                    finish_receipt(current)
                    return True
                current.state = "silent"
                current.error = "模型判断为广告，保持静默" if result.is_advertisement else (
                    "模型判断无需回复" if not result.should_reply else "模型未生成可发送内容")
                logger.info("聊天未发送 turn=%s group=%s reason=%s", turn.id, turn.group_id, current.error)
                return True
            cfg = GroupSettings.model_validate(group.settings)
            options = await self.stickers.options(session, turn.group_id, cfg)
            _, sticker_state = await self.stickers.state(session, turn.group_id)
        if result.sticker and emoji_key(result.sticker) in options:
            if (
                not selected_sticker
                or selected_sticker["set_name"].lower()
                not in {name.lower() for name in cfg.chat_sticker_packs}
                or selected_sticker["file_unique_id"] in sticker_state.get("recent", [])
            ):
                selected_sticker = await self.stickers.choose(
                    cfg.chat_sticker_packs,
                    result.sticker,
                    sticker_state.get("recent", []),
                )
        else:
            selected_sticker = None
        if not selected_sticker and not result.text:
            async with self.db.transaction() as session:
                current = await session.get(ChatTurn, turn.id)
                current.state = "silent"
                current.error = "没有匹配的可发送贴纸"
                logger.info("聊天未发送 turn=%s group=%s reason=%s", turn.id, turn.group_id, current.error)
            return True
        async with self.db.transaction() as session:
            current, group, valid = await self.context.valid_turn(session, turn, original, sender_id, generated.context)
            if not valid:
                if current.state not in ("retracting", "retracted", "retraction_failed"):
                    current.state = "skipped"
                    current.error = "发送前，聊天设置、目标消息或引用上下文已失效"
                logger.info("聊天未发送 turn=%s group=%s reason=%s", turn.id, turn.group_id, current.error)
                return True
            latest_cfg = GroupSettings.model_validate(group.settings)
            rules = (await session.scalars(select(Rule).where(Rule.group_id == turn.group_id))).all()
            reasons = output_reasons(
                result.text,
                [row_dict(rule) for rule in rules],
                group.settings,
                getattr(self.tg, "username", ""),
            )
            if reasons:
                current.state, current.response = "skipped", ""
                current.error = "AI 回复已拦截：" + "；".join(reasons)[:600]
                logger.warning("AI 回复已拦截 turn=%s group=%s reasons=%s", turn.id, turn.group_id, reasons)
                return True
            if selected_sticker and (
                not latest_cfg.chat_stickers
                or selected_sticker["set_name"].lower()
                not in {name.lower() for name in latest_cfg.chat_sticker_packs}
            ):
                selected_sticker = None
            if not selected_sticker and not result.text:
                current.state = "silent"
                current.error = "发送前贴纸设置已变更，无可发送内容"
                logger.info("聊天未发送 turn=%s group=%s reason=%s", turn.id, turn.group_id, current.error)
                return True
            current.response = json.dumps(
                {
                    "text": result.text,
                    "sticker": result.sticker,
                    "is_advertisement": result.is_advertisement,
                    "selected_sticker": selected_sticker,
                    "context": fingerprint,
                    "source_context": generated.context,
                    "text_message_id": text_message_id,
                },
                ensure_ascii=False,
            )
            current.state = "sending" if result.text and not text_message_id else "generating"
        # No database writer or moderation worker waits on the model or Telegram send.
        kwargs = {
            "chat_id": turn.group_id,
            "reply_parameters": {"message_id": turn.message_id, "allow_sending_without_reply": False},
        }
        if turn.topic_id:
            kwargs["message_thread_id"] = turn.topic_id
        if result.text and not text_message_id:
            try:
                sent = await self.tg.call(
                    "send_message", **kwargs, text=result.text, link_preview_options={"is_disabled": True}
                )
            except TelegramBadRequest as exc:
                if await self.missing_target(turn, exc):
                    return True
                raise
            text_message_id = sent["message_id"]
            async with self.db.transaction() as session:
                current = await session.get(ChatTurn, turn.id)
                await self.context.remember_output(
                    session, turn, cfg, group, sent, sender_id, text=result.text
                )
                if current.state in ("retracting", "retracted", "retraction_failed"):
                    return True
                cached = json.loads(current.response)
                cached["text_message_id"] = text_message_id
                current.response = json.dumps(cached, ensure_ascii=False)
                current.state = "generating"
        if selected_sticker:
            async with self.db.transaction() as session:
                current, group, valid = await self.context.valid_turn(session, turn, original, sender_id, generated.context)
                if not valid:
                    if current.state not in ("retracting", "retracted", "retraction_failed"):
                        current.state = "skipped"
                        current.error = "发送贴纸前，聊天设置、目标消息或引用上下文已失效"
                    logger.info("聊天未发送 turn=%s group=%s reason=%s", turn.id, turn.group_id, current.error)
                    return True
                latest_cfg = GroupSettings.model_validate(group.settings)
                options = await self.stickers.options(session, turn.group_id, latest_cfg)
                if emoji_key(result.sticker) not in options or selected_sticker["set_name"].lower() not in {
                    name.lower() for name in latest_cfg.chat_sticker_packs
                }:
                    selected_sticker = None
                else:
                    current.state = "sending"
        if selected_sticker:
            if not await self.send_sticker(turn, cfg, group, sender_id, selected_sticker, text_message_id, kwargs):
                return True
        async with self.db.transaction() as session:
            current = await session.get(ChatTurn, turn.id)
            if current.state in ("retracting", "retracted", "retraction_failed"):
                return True
            current.state = "done" if current.sent_message_id is not None else "silent"
            current.error = "" if current.state == "done" else "贴纸发送前设置已变更，无可发送内容"
            finish_receipt(current)
            logger.info(
                "聊天发送结束 turn=%s group=%s state=%s sent_message=%s reason=%s",
                turn.id, turn.group_id, current.state, current.sent_message_id, current.error,
            )
        return True

    async def send_sticker(self, turn, cfg, group, sender_id, selected_sticker, text_message_id, kwargs):
        try:
            sent = await self.tg.call("send_sticker", **kwargs, sticker=selected_sticker["file_id"])
        except TelegramBadRequest as exc:
            if await self.missing_target(turn, exc):
                return False
            await self.stickers.rejected(
                turn.group_id,
                "文字已发送，贴纸被 Telegram 拒绝，本次已略过贴纸"
                if text_message_id
                else "贴纸发送被 Telegram 拒绝，暂时停用贴纸",
            )
            if not text_message_id:
                raise
        else:
            async with self.db.transaction() as session:
                await self.context.remember_output(
                    session, turn, cfg, group, sent, sender_id, sticker=selected_sticker
                )
        return True
