"""Explain the actual enforcement policy identically in Bot and HTTP views."""

from purrai.admin_labels import KINDS, MODES, SCOPES


def penalty_label(value):
    return {"delete": "只删除消息", "permanent_mute": "删除消息并永久禁言", "ban": "删除消息并永久封禁"}.get(value, "只删除消息")


def describe_rule(rule):
    kind = rule["kind"]
    if kind == "block_emoji_pack":
        matching = "消息正文、说明文字或贴纸中的表情包来源精确匹配，不检查昵称旁状态"
        consequence = "使用该表情包时" + penalty_label(rule.get("penalty", "delete"))
    elif kind == "block_target":
        matching = "推广目标精确匹配（网站按已记录域名，短链仅限该链接）"
        consequence = penalty_label(rule.get("penalty", "ban")) + "；明确举报引用仅删除。管理员及免自动处罚白名单豁免"
    elif kind.startswith("allow_"):
        matching = "账号匹配" if kind == "allow_mention" else "域名与子域名"
        consequence = "仅允许对应账号或链接，其他广告、昵称及刷屏规则继续检查"
    elif kind != "keyword":
        matching = "账号匹配" if kind == "block_mention" else "域名与子域名"
        consequence = "正文命中删除消息；用户名命中" + penalty_label(rule.get("name_penalty", "permanent_mute"))
        if kind == "block_domain":
            consequence = "正文链接命中删除消息"
    else:
        matching = MODES.get(rule.get("mode"), rule.get("mode", ""))
        body = penalty_label(rule.get("penalty", "delete"))
        name = penalty_label(rule.get("name_penalty", "permanent_mute"))
        consequence = {
            "message": f"正文命中{body}",
            "name": f"昵称或用户名命中{name}",
            "either": f"昵称或用户名命中{name}；正文命中{body}",
            "name_and_message": f"昵称与正文同时命中才{name}",
        }.get(rule.get("scope", "message"), "")
        if rule.get("penalty") == "ai_confirmed_mute":
            consequence = "所选范围内任一关键词命中且AI确认广告，删除消息并永久禁言；关键词本身不处罚，独立审核不受聊天参与条件限制"
    return {**rule, "kind_label": KINDS.get(kind, kind), "matching_label": matching,
            "scope_label": SCOPES.get(rule.get("scope"), "正文"), "consequence": consequence}
