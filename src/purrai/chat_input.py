"""Deterministic chat exclusions; these checks never punish a group member."""

import re

from purrai.chat_safety import LINKS
from purrai.rules import assess, compact, normalize

TRADE = r"(?:跑分|水房|灰产|灰经验|代收|代付|收款码|车队)"
RECRUIT = re.compile(rf"^(?:来[个点些几位名]*|找[个些几位名]*|急招|招聘|招募|诚招|长期招).{{0,28}}{TRADE}"
                     rf"|^{TRADE}.{{0,24}}(?:招人|招募|招聘|收人|无押|无压|日结|经验优先)")
OFFER = re.compile(r"(?:带单|返佣|代刷|代充|代办|兼职|搬砖|策略频道|投资群|套利|开户|注册|赚钱)")
CONTACT = re.compile(r"(?:私聊|私信|联系我|联系老板|加我|看我简介|点我头像|进我主页|扫码|免费进群|免费进频道)")
FOMO = re.compile(r"(?:花钱|付费|收费).{0,16}(?:频道|群).{0,16}(?:现在|如今|今天).{0,12}(?:免费|白送|直接进|随便进)")
PROMISE = re.compile(r"(?:稳赚|包赚|保本收益|躺赚|零风险|日[入赚]\d+)")


def advertising(text, *, direct=False):
    canonical = normalize(text)
    # Split clauses first, then remove spacing, invisible characters and emoji.
    # A quoted warning or a real question about scams is not a recruitment call.
    clauses = [compact(re.sub(r"@[a-z0-9_]+", "", clause))
               for clause in re.split(r"[\n。！？!?；;，,]", canonical)]
    value = compact(re.sub(r"@[a-z0-9_]+", "", canonical))
    question = direct and re.search(
        r"(?:怎么识别|如何识别|是不是(?:广告|诈骗|骗局)|这是不是|这算不算|为什么.{0,12}(?:不能|不要|危险)|是真的吗)", value)
    if question:
        return ""
    if any(RECRUIT.search(clause) for clause in clauses + [value]):
        return "广告招揽：灰产或跑分招聘"
    if OFFER.search(value) and CONTACT.search(value):
        return "广告招揽：业务推广并要求联系或引流"
    if FOMO.search(value) and not (direct and re.search(r"(?:怎么看|是不是|为什么|是真的吗|合理吗)", value)):
        return "广告招揽：付费群或频道免费引流话术"
    if PROMISE.search(value) and (CONTACT.search(value) or re.search(r"(?:加群|进群|注册|开户|立即加入)", value)):
        return "广告招揽：收益承诺并要求加入或联系"
    return ""


def input_reasons(text, rules, settings, *, direct=False, urls=()):
    canonical = normalize(text)
    found = [match.group().rstrip(".,，。!?！？);；:：]】}）\"'") for match in LINKS.finditer(canonical)]
    reasons = assess(canonical, list(urls) + found, rules, settings)["reasons"]
    name = normalize(settings.get("chat_name", ""))
    body = canonical.removeprefix(name).lstrip(" ，,：:") if name else canonical
    reason = advertising(body, direct=direct)
    return list(dict.fromkeys(reasons + ([reason] if reason else [])))
