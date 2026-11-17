"""Apply configured advertising rules to biography evidence without changing body scopes."""

from purrai.business_rules import BusinessReason, with_rules


def assess_biography(value, rules, settings):
    from purrai.rules import assess, message_content
    # Existing keyword scopes retain their meaning. Administrators explicitly
    # select biography for a keyword; target/domain policies apply to both sources.
    selected = with_rules(rules, [{**rule, "scope": "message"} if rule["kind"] == "keyword" else rule
                                  for rule in rules if rule["kind"] != "keyword" or rule.get("scope") == "bio"])
    text, urls = message_content({"text": value})
    result = assess(text, urls, selected, {**settings, "newcomer_wallets": False})
    reasons = []
    for reason in result["reasons"] + result["name_reasons"]:
        reasons.append(BusinessReason("用户简介：" + str(reason), getattr(reason, "action", "") or "delete",
                       getattr(reason, "rule_id", ""), getattr(reason, "advertiser_cooldown", False)))
    return reasons, result["rule_ids"]
