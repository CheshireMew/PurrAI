"""Deterministic chat exclusions; these checks never punish a group member."""

import re

from purrai.chat_safety import LINKS
from purrai.advertising_patterns import ad_text, authored_text, discussion
from purrai.rules import assess, compact, normalize

TRADE = r"(?:跑分|水房|灰产|灰经验|代收|代付|收款码|车队)"
RECRUIT = re.compile(rf"^(?:来[个点些几位名]*|找[个些几位名]*|急招|招聘|招募|诚招|长期招).{{0,28}}{TRADE}"
                     rf"|^{TRADE}.{{0,24}}(?:招人|招募|招聘|收人|无押|无压|日结|经验优先)")
OFFER = re.compile(r"(?:带单|返佣|代刷|代充|代办|兼职|搬砖|策略频道|投资群|套利|开户|注册|赚钱)")
CONTACT = re.compile(r"(?:私聊|私信|联系我|联系老板|加我|看我简介|点我头像|进我主页|扫码|免费进群|免费进频道)")
FOMO = re.compile(r"(?:花钱|付费|收费).{0,16}(?:频道|群).{0,16}(?:现在|如今|今天).{0,12}(?:免费|白送|直接进|随便进)")
PROMISE = re.compile(r"(?:稳赚|包赚|保本收益|躺赚|零风险|日[入赚]\d+)")


def solicitation(text):
    """Additional chat-only exclusions; never evidence for a member penalty."""
    canonical = ad_text(text)
    if discussion(canonical):
        return ""
    value = compact(authored_text(canonical))
    # Questions, reports and quoted examples are different from authored offers.
    if re.search(r"怎么|如何|是不是|是真的吗|怎么办|可信吗|骗局|诈骗|小心|警惕|不要|别信", value):
        return ""
    if re.fullmatch(r"来回血", value):
        return "广告招揽：回血口令"
    if (re.search(r"(?:高返|高反|返佣|反佣)\s*\d{1,3}\s*[%％]", canonical)
            and re.search(r"群|福利|策略|带单", value)):
        return "广告招揽：高比例返佣引流"
    if (re.search(r"策略|带单|胜率|赚.{0,6}倍|\d+天.{0,6}倍", value)
            and re.search(r"点头像|戳[主竹][页叶]|进来捡钱|赶快进|速进|免费来|天天发单", value)):
        return "广告招揽：交易收益或策略引流"
    if re.search(r"社[群裙].{0,12}(?:好准|赚|盈利)", value) and re.search(r"进来|捡钱", value):
        return "广告招揽：社群收益引流"
    if re.search(r"新澳|[六6][合和][彩采]", value) and re.search(r"内[幕慕]|频道|开奖|下注", value):
        return "广告招揽：博彩频道推广"
    if re.search(r"\d{2,5}[全佺]套.{0,8}(?:无限火力|面付|上门)", value):
        return "广告招揽：色情服务报价"
    if re.fullmatch(r"\d+(?:\.\d+)?\s*trx\s*闪租", canonical):
        return "广告招揽：能量租赁报价"
    if re.fullmatch(r"(?:苹果|水果机)\d{1,2}(?:只要|仅售|特价)\d+(?:k|千|元|块)", value):
        return "广告招揽：整条商品促销报价"
    if re.search(r"副业|增收|兼职", value) and re.search(r"(?:一天|日入|日赚)[0-9o]{3,}", value):
        return "广告招揽：高收入副业招揽"
    return ""


def advertising(text, *, direct=False):
    canonical = ad_text(text)
    extra = solicitation(canonical)
    if extra:
        return extra
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
    reasons = assess(text, list(urls) + found, rules, settings)["reasons"]
    name = normalize(settings.get("chat_name", ""))
    body = canonical.removeprefix(name).lstrip(" ，,：:") if name else canonical
    reason = advertising(body, direct=direct)
    return list(dict.fromkeys(reasons + ([reason] if reason else [])))
