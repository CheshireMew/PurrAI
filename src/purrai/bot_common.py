"""Pure Telegram presentation and validation helpers."""

import unicodedata
from datetime import datetime
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from purrai.admin_labels import BOOLEANS, NUMBERS


def keyboard_rows(buttons):
    if len(buttons) < 6:
        return [[item] for item in buttons]
    rows, pair = [], []
    for item in buttons:
        label = item["text"]
        width = sum(2 if unicodedata.east_asian_width(char) in "WF" else 1 for char in label)
        if width > 32 or label.startswith(("返回", "取消", "切换群")):
            if pair:
                rows.append(pair)
                pair = []
            rows.append([item])
        else:
            pair.append(item)
            if len(pair) == 2:
                rows.append(pair)
                pair = []
    if pair:
        rows.append(pair)
    return rows


def clock(value):
    return (
        datetime.fromtimestamp(value, ZoneInfo("Asia/Shanghai")).strftime("%m-%d %H:%M:%S") if value else "—"
    )


def button(label, action, **data):
    return label, action, data


def explain(error):
    if isinstance(error, ValidationError):
        names = {
            **BOOLEANS,
            **NUMBERS,
            "value": "规则内容",
            "note": "备注",
            "reason": "原因",
            "user_id": "用户 ID",
            "id": "管理员 ID",
            "name": "管理员备注",
        }
        details = []
        for item in error.errors():
            field = str(item["loc"][-1]) if item["loc"] else "输入"
            name, context = names.get(field, "输入"), item.get("ctx", {})
            if item["type"] == "greater_than_equal":
                message = f"不能小于 {context['ge']}"
            elif item["type"] == "less_than_equal":
                message = f"不能大于 {context['le']}"
            elif item["type"] == "greater_than":
                message = f"必须大于 {context['gt']}"
            elif item["type"] == "string_too_long":
                message = f"不能超过 {context['max_length']} 个字符"
            elif item["type"] == "string_too_short":
                message = "不能为空"
            else:
                message = str(context.get("error", "格式不正确，请检查后重试"))
            details.append(f"{name}：{message}")
        return "输入不符合要求：" + "；".join(details)
    return str(error)
