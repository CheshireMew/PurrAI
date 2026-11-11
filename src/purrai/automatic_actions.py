"""Automatic moderation actions selected by the action dispatcher."""


from purrai.shared_policy import effective_rules
from purrai.advertising_guard import link_reasons
from purrai.advertising_patterns import is_ban_reason, source_titles
from purrai.business_rules import BusinessEngine
from purrai.sender_identity import sender_name
from purrai.message_identity import message_sender
from purrai.group_link_identity import own_channel_id, own_link_keys
from purrai.rules import message_content, normalize
from purrai.telegram_sources import channel_sources, external_channel_reasons
from purrai.verification_state import matches_action
from purrai.ai_advertising import advertisement_count, LIMIT
from purrai.schemas import GroupSettings


class AutomaticActions:
    def __init__(self, restrictions):
        self.restrictions = restrictions

    async def prepare(self, session, action, group, verification, member):
        if action.kind == "profile_mute" and not action.payload.get("generation"):
            action.state, action.error = "cancelled", "旧昵称处罚缺少入群批次，已停止执行；请重新核实成员"
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
        reasons = []
        if group.settings.get("advertising_ban", True):
            engine = BusinessEngine(rules)
            text = normalize(message_content(message)[0])
            hits = engine.run("profile", sender_name(message_sender(message), include_username=True))
            hits += [hit for stage in ("short", "advertising") for hit in engine.run(
                stage, text, source_titles=[normalize(title) for title in source_titles(message)], urls=message_content(message)[1])]
            reasons = [hit.reason for hit in hits if hit.action == "ban"]
        from purrai.promotion_targets import blacklist_reasons, independent_target_rules
        independent = await independent_target_rules(session, rules, group.id, message.get("message_id"))
        reasons += blacklist_reasons(message, independent, settings=group.settings, evidence=evidence.get("links", []))
        reasons += link_reasons(message, group, independent, await own_link_keys(session, group.id), evidence.get("links", []))
        if group.settings.get("external_group_ban", True):
            reasons += external_channel_reasons(channel_sources(message), group.id, group.settings, rules,
                                                linked_channel_id=await own_channel_id(session, group.id))
        if not any(is_ban_reason(reason) for reason in reasons):
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
        if (not GroupSettings.model_validate(group.settings).chat_advertiser_cooldown or not member
                or not member.present or member.generation != action.payload.get("generation")
                or await advertisement_count(session, group.id, member.user_id) < LIMIT):
            action.state, action.error = "cancelled", "AI广告处理已关闭或计数、成员状态已变化"
            return
        await self.restrictions.permanent_profile_mute(session, group, member, action, "ai_ad")
