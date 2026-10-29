"""Telegram pack defaults and parsing, independent of persistence and workers."""

import re
from urllib.parse import urlsplit

DEFAULT_BLOCKED_PACKS = ("bygbb666", "gshechaba", "tgvip_16")
DEFAULT_CHAT_PACKS = (
    "myadestes",
    "Beach_Bat_Responses_by_fStikBot",
    "poi466",
    "LLMG_GIF",
    "cookprivet",
    "tofupack_by_fStikBot",
    "catvideopack_by_fStikBot",
    "superCheshire",
    "sumyy3",
    "HMS_Cheshire",
    "p1_wmtz1349120938_by_WuMingv2Bot",
    "Cheshire_the_kitten",
    "sticker_ab2f7c92_by_moe_sticker_bot",
    "sese11_by_WuMingv2Bot",
    "p0_wmtz1678979585_by_WuMingv2Bot",
    "p0_wmtz1296407908_by_WuMingv2Bot",
    "GreatCheshire",
    "BigCheshire",
    "goodCheshire",
    "AwesomeCheshireCat",
)
DEFAULT_ANTIFRAUD_PACKS = ("FTX888",)


def _parse(value, route, *, strict_https, link_error, name_error):
    value = value.strip()
    if "://" in value or value.startswith("t.me/"):
        parsed = urlsplit(value if "://" in value else "https://" + value)
        parts = parsed.path.strip("/").split("/")
        valid_host = (
            (parsed.scheme == "https" and parsed.netloc.lower() == "t.me")
            if strict_https
            else (parsed.hostname in ("t.me", "www.t.me"))
        )
        if not valid_host or len(parts) != 2 or parts[0] != route:
            raise ValueError(link_error)
        value = parts[1]
    if not re.fullmatch(r"[A-Za-z0-9_]{1,64}", value):
        raise ValueError(name_error)
    return value


def emoji_pack_name(value):
    return _parse(
        value,
        "addemoji",
        strict_https=True,
        link_error="请填写 https://t.me/addemoji/表情包名称，不能填写发布者的 @用户名",
        name_error="请填写表情包短名称或 t.me/addemoji 链接",
    ).lower()


def sticker_pack_name(value):
    return _parse(
        value,
        "addstickers",
        strict_https=False,
        link_error="聊天贴纸请填写 t.me/addstickers 链接或贴纸包短名称",
        name_error="贴纸包短名称只能包含字母、数字和下划线，最长 64 个字符",
    )
