"""Explain the actual enforcement policy identically in Bot and HTTP views."""

from purrai.admin_labels import KINDS, MODES, SCOPES


def describe_rule(rule):
    kind = rule["kind"]
    if kind == "block_target":
        matching = "推广目标精确匹配（网站按已记录域名，短链仅限该链接）"
        consequence = "删除消息并永久封禁；明确举报引用仅删除。管理员及免自动处罚白名单豁免"
    elif kind.startswith("allow_"):
        matching = "账号匹配" if kind == "allow_mention" else "域名与子域名"
        consequence = "仅允许对应账号或链接，其他广告、昵称及刷屏规则继续检查"
    elif kind != "keyword":
        matching = "账号匹配" if kind == "block_mention" else "域名与子域名"
        consequence = "正文命中删除消息；禁止账号作为发言者用户名时永久禁言并删除消息"
        if kind == "block_domain":
            consequence = "正文链接命中删除消息"
    else:
        matching = MODES.get(rule.get("mode"), rule.get("mode", ""))
        body = "删除消息并永久禁言" if rule.get("penalty") == "permanent_mute" else "只删除消息"
        consequence = {
            "message": f"正文命中{body}",
            "name": "昵称或用户名命中永久禁言；发言时删除对应消息",
            "either": f"昵称或用户名命中永久禁言；正文命中{body}",
            "name_and_message": "昵称与正文同时命中才永久禁言并删除消息",
        }.get(rule.get("scope", "message"), "")
    return {**rule, "kind_label": KINDS.get(kind, kind), "matching_label": matching,
            "scope_label": SCOPES.get(rule.get("scope"), "正文"), "consequence": consequence}
