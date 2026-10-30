"""High precision solicitation evidence; ordinary market vocabulary is insufficient."""

import re
import unicodedata

from purrai.telegram_sources import CHANNEL_PREFIX

AD_PREFIX = "明确广告引流："
GROUP_PREFIX = "外群链接引流："

INVITATION = re.compile(r"(?:来|加入|进入|进|加|欢迎加入|邀请.{0,4}加入)(?:我们|我的|咱们|这个|内部|内|实战|学习|交流|策略|投资|合约|交易|vip|贵宾|免费|的){0,8}(?:群|频道)")
TRADING = re.compile(r"跟单|带单|开仓|合约|币圈|btc|eth|比特币|以太坊|交易|策略|老师.{0,6}分析|分析.{0,6}逻辑")
PITCH = re.compile(r"(?:每天|每日|实时|免费).{0,8}(?:精准分析|跟单|带单|策略|指导)|(?:跟单|带单|合约).{0,8}(?:免费|实时策略|稳赚)|让每一次开仓.{0,8}依据")
SOURCE_AD = re.compile(r"免费.{0,8}(?:跟单|带单)|(?:跟单|带单).{0,8}(?:实时策略|稳赚|盈利)|(?:合约|投资).{0,8}(?:稳赚|保本)")
CONTACT = re.compile(r"私聊我|私信我|联系我|联系老板|加我|看我简介|点我头像|进我主页|扫码加入|扫码进群")
OFFER = re.compile(r"带单|返佣|代刷|代充|跑分|水房|套利项目|投资群|策略频道")
PROMISE = re.compile(r"稳赚|包赚|零风险|保本收益|躺赚|日[入赚]\d+")
WARNING = re.compile(r"(?:举报|曝光|防骗|诈骗|骗局|骗子|骗人|广告).{0,16}(?:提醒|识别|小心|警惕|别|不要|请管理员|处理|是真的吗|怎么看|是不是)|(?:怎么|如何)(?:识别|辨别|举报)|(?:这个|这种|有人|刚看到|看到有人|所谓).{0,36}(?:广告|诈骗|骗局|骗子|骗钱|别进|不要加)|请管理员.{0,12}(?:处理|看看|删)")
AD_VARIANTS = str.maketrans({
    "賺": "赚", "錢": "钱", "著": "着", "頁": "页", "區": "区", "約": "约",
    "倉": "仓", "說": "说", "對": "对", "別": "别", "後": "后", "週": "周",
    "來": "来", "強": "强", "勝": "胜", "滿": "满", "莊": "庄", "點": "点",
    "頭": "头", "費": "费", "進": "进", "發": "发", "轉": "转", "機": "机",
    "個": "个", "學": "学", "葉": "叶", "頻": "频", "內": "内", "無": "无",
    "們": "们", "這": "这", "準": "准", "撿": "捡", "簡": "简", "聯": "联",
    "繫": "系", "實": "实", "時": "时", "穩": "稳", "險": "险", "風": "风",
    "誘": "诱", "騙": "骗", "報": "报", "傳": "传", "問": "问", "麼": "么",
    "樣": "样", "單": "单", "帶": "带", "隨": "随", "導": "导", "號": "号",
    "廣": "广", "種": "种", "嗎": "吗",
})
PROFILE_DESTINATION = re.compile(
    r"(?:我(?:的)?|本人)(?:主页|简介|置顶|头像)(?:里面|里|上|中)?(?:的|推荐)?(?:社区|社群|群|频道)"
    r"|(?:看|点|戳|进|去|查看|打开)(?:我(?:的)?)?(?:主页|简介|头像)"
)
PROFILE_FOLLOW = re.compile(r"跟着|跟随|跟对|跟单|带单")
PROFILE_GAIN = re.compile(
    r"又赚|赚(?:到|了)?钱|翻[仓倍]|(?:盈利|收益)(?:不错|很好|稳定|增加|提升|翻|[0-9一二三四五六七八九十百千万])"
    r"|赚.{0,8}(?:倍|万|[0-9])|[0-9一二三四五六七八九十]+倍"
)
PROFILE_REPORT = re.compile(
    r"^(?:举报|曝光|刚看到|看到有人|有人(?:发|说|宣传)|这(?:条|句|种).{0,12}(?:广告|话术))"
    r"|(?:不要|别|不能|勿)(?:信|相信|跟|进|加)|(?:是否|是不是|是真的吗|可信吗|怎么看|怎么识别)"
)


def ad_text(text):
    """Normalize advertising evidence; wallet recognition uses the original text."""
    return "".join(char for char in unicodedata.normalize("NFKC", text).casefold()
                   if unicodedata.category(char) != "Cf").translate(AD_VARIANTS)


def profile_promotion(text):
    value = compact(authored_text(text))

    def affirmed(pattern):
        return any(not re.search(r"没有|并没|不再|不|没|未|无", value[max(0, hit.start() - 3):hit.start()])
                   for hit in pattern.finditer(value))

    return bool(PROFILE_DESTINATION.search(value) and affirmed(PROFILE_FOLLOW)
                and TRADING.search(value) and affirmed(PROFILE_GAIN)
                and not PROFILE_REPORT.search(value))


def compact(value):
    return "".join(char for char in value if char.isalnum())


def authored_text(text):
    return re.sub(r'“[^”]*”|「[^」]*」|『[^』]*』|"[^"\n]*"|(?m:^\s*>.*$)', '', text)


def contact_audience(text):
    for clause in re.split(r"[\n。！？!?；;，,]", authored_text(text)):
        value = compact(clause)
        if re.match(r"有人|他|她|老师|这个人|那个|所谓|怎么|如何|请问|我看到|我遇到", value):
            continue
        for hit in CONTACT.finditer(value):
            if not re.search(r"不要|不用|禁止|拒绝|无需|不必|别", value[max(0, hit.start() - 4):hit.start()]):
                return True
    return False


def invites_audience(text):
    for clause in re.split(r"[\n。！？!?；;，,]", text):
        value = compact(clause)
        if re.match(r"我|哪里|有没有|怎么|如何|能不能|是否|请问|谁知道", value):
            continue
        hit = INVITATION.search(value)
        if hit and (hit.start() == 0 or re.match(r"欢迎|大家|想|有兴趣|感兴趣|可以|请|赶快|赶紧|现在|免费", value)):
            if re.search(r"内部|策略|投资|合约|交易|vip|贵宾|频道", hit.group()) or PITCH.search(value) or CONTACT.search(value):
                return True
    return False


def discussion(text):
    # Inspect the author's words outside quoted examples. "不是广告，来进群"
    # never grants an exemption; a renewed invitation overrides a disclaimer.
    text = ad_text(text)
    authored = authored_text(text)
    value = compact(authored)
    if invites_audience(authored) or contact_audience(authored):
        return False
    return bool(re.match(r"^\s*(?:举报|曝光|请管理员(?:处理|看看)|刚看到有人发广告)\s*[:：]", text)
                or WARNING.search(value) or re.search(r"是不是(?:骗子|诈骗|骗局|广告)|这算不算(?:广告|诈骗)", value))


def advertising_reasons(text, source_titles=()):
    """Shared evidence used at assessment and again before an actual ban."""
    text = ad_text(text)
    if discussion(text):
        return []
    authored = authored_text(text)
    value = compact(authored)
    if profile_promotion(authored):
        return [AD_PREFIX + "主页社群引流并以跟随交易收益诱导"]
    if invites_audience(authored) and (TRADING.search(value) or PITCH.search(value)):
        return [AD_PREFIX + "招揽加入交易群或策略频道"]
    if contact_audience(authored) and (OFFER.search(value) or PROMISE.search(value)):
        return [AD_PREFIX + "业务或收益招揽并要求联系"]
    if PROMISE.match(value) and re.search(r"立即加群|立即进群|立即加入|立即注册|欢迎加入|扫码加入", value):
        return [AD_PREFIX + "收益承诺并要求加入"]
    if any(SOURCE_AD.search(compact(ad_text(title))) for title in source_titles) and PITCH.match(value):
        return [AD_PREFIX + "跟单广告来源配合交易招揽正文"]
    return []


def is_ban_reason(reason):
    return reason.startswith((AD_PREFIX, GROUP_PREFIX, CHANNEL_PREFIX))


def source_titles(message):
    found = []
    for item in (message, message.get("reply_to_message", {}), message.get("external_reply", {})):
        origin = item.get("forward_origin") or item.get("origin") or {}
        for chat in (origin.get("chat", {}), origin.get("sender_chat", {}),
                     item.get("forward_from_chat", {}), item.get("sender_chat", {})):
            if chat.get("type") in ("channel", "supergroup", "group") and chat.get("title"):
                found.append(chat["title"])
    return found
