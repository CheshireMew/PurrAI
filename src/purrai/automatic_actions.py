"""Automatic moderation actions selected by the action dispatcher."""


from purrai.shared_policy import effective_rules
from purrai.advertising_guard import link_reasons
from purrai.advertising_patterns import is_ban_reason
from purrai.group_link_identity import own_channel_id, own_link_keys
from purrai.telegram_sources import channel_sources, external_channel_reasons
from purrai.verification_state import matches_action
from purrai.ai_advertising import cumulative_penalty_applies, keyword_penalty_applies
from purrai.schemas import GroupSettings
from purrai.message_buttons import has_inline_buttons
from purrai.advertising_constants import BUTTON_REASON


class AutomaticActions:
    def __init__(self, restrictions):
        self.restrictions = restrictions

    async def prepare(self, session, action, group, verification, member):
        if action.kind == "profile_mute" and not action.payload.get("generation"):
            action.state, action.error = "cancelled", "旧昵称处罚缺少入群批次，已停止执行；请重新核实成员"
            return False
        if action.kind in ("contact_ban", "advertisement_ban") and action.payload.get("violation_id"):
            from purrai.models import Violation
            from purrai.reason_policy import classifies
            violation = await session.get(Violation, action.payload["violation_id"])
            rules = await effective_rules(session, group.id)
            category = "contact" if action.kind == "contact_ban" else "ban"
            if violation and not any(classifies(item, category, rules, violation.reason_details) for item in violation.reasons):
                action.state, action.error = "cancelled", "历史判定分类已变化，未执行旧处罚"
                return False
        if not matches_action(action, verification, member):
            action.state = "cancelled"
            return False
        return True

    async def contact_ban(self, session, action, group, verification, member):
        if not group.enabled or not member or not member.present:
            action.state = "cancelled"
            return
        if group.settings.get("contact_ads_only", False):
            from purrai.promotion_targets import blacklist_reasons
            from purrai.solicitation_rules import contact_should_block
            message = (action.payload.get("contact_evidence") or {}).get("message")
            rules = await effective_rules(session, group.id)
            if not message or not (contact_should_block(message, group.settings, rules)
                                   or blacklist_reasons(message, rules, settings=group.settings)):
                action.state, action.error = "cancelled", "联系人缺少广告或黑名单证据，未执行封禁"
                return
        await self.restrictions.permanent_ban(session, group, member, action)

    async def advertisement_ban(self, session, action, group, verification, member):
        if not member or not member.present:
            action.state = "cancelled"
            return
        evidence = action.payload.get("ad_evidence") or {}
        message = evidence.get("message", {})
        rules = await effective_rules(session, group.id)
        reasons = [BUTTON_REASON] if group.settings.get("inline_buttons_ban", False) and has_inline_buttons(message) else []
        from purrai.promotion_targets import blacklist_reasons, independent_target_rules
        independent = await independent_target_rules(session, rules, group.id, message.get("message_id"))
        from purrai.rule_evaluation import evaluate_message
        from purrai.message_emoji import cached_packs
        packs, _ = await cached_packs(session, message)
        reasons += evaluate_message(message, group.settings, independent, emoji_packs=packs).ordinary["ban_reasons"]
        reasons += blacklist_reasons(message, independent, settings=group.settings, evidence=evidence.get("links", []))
        reasons += link_reasons(message, group, independent, await own_link_keys(session, group.id), evidence.get("links", []))
        if group.settings.get("external_group_ban", True):
            reasons += external_channel_reasons(channel_sources(message), group.id, group.settings, rules,
                                                linked_channel_id=await own_channel_id(session, group.id))
        if not any(is_ban_reason(reason, rules) for reason in reasons):
            action.state, action.error = "cancelled", "广告或链接策略已变化，未执行永久封禁"
            return
        await self.restrictions.permanent_ban(session, group, member, action)

    async def emoji_ban(self, session, action, group, verification, member):
        await self.restrictions.ban_emoji_status(session, group, member, action)

    async def contact_kick(self, session, action, group, verification, member):
        if not group.enabled or not member or not member.present and not action.payload.get("kick_until"):
            action.state = "cancelled"
            return
        await self.restrictions.kick_member(session, group, member, action, "contact_kick")

    async def legacy_mute(self, session, action, group, verification, member):
        action.state = "cancelled"
        return

    async def emoji_mute(self, session, action, group, verification, member):
        await self.restrictions.mute_emoji_status(session, group, member, action)

    async def profile_mute(self, session, action, group, verification, member):
        await self.restrictions.permanent_profile_mute(session, group, member, action, "profile")

    async def rule_mute(self, session, action, group, verification, member):
        if not group.enabled:
            action.state = "cancelled"
            return
        await self.restrictions.permanent_profile_mute(session, group, member, action, "rule")

    async def ai_ad_mute(self, session, action, group, verification, member):
        qualifies = (await keyword_penalty_applies(session, action, group)
                     if "ai_keyword_evidence" in action.payload
                     else bool(member and await cumulative_penalty_applies(session, action, group)))
        if ((not GroupSettings.model_validate(group.settings).chat_advertiser_cooldown
             and "advertisement_evidence" not in action.payload) or not member
                or not member.present or member.generation != action.payload.get("generation")
                or not qualifies):
            action.state, action.error = "cancelled", "AI广告处理已关闭或计数、成员状态已变化"
            return
        await self.restrictions.permanent_profile_mute(session, group, member, action, "ai_ad")

    async def sample_ad_mute(self, session, action, group, verification, member):
        from purrai.advertisement_store import evidence_applies
        if (not member or not member.present or member.generation != action.payload.get("generation")
                or not await evidence_applies(session, action, group)):
            action.state, action.error = "cancelled", "广告样本或消息已失效"
            return
        await self.restrictions.permanent_profile_mute(session, group, member, action, "sample_ad")
