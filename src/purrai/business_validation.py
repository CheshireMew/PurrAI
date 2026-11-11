"""Validate editable policy documents before they reach the evaluator."""

import copy
import json
import re

from purrai.business_rules import compiled, transform


def validate_document(document):
    def require(ok, message):
        if not ok:
            raise ValueError(message)

    def keys(value, required, optional=()):
        require(isinstance(value, dict) and set(required) <= value.keys() <= set(required) | set(optional),
                "规则字段缺失或包含不支持的字段")

    def label(value, limit=160):
        require(isinstance(value, str) and 0 < len(value.strip()) <= limit, "名称或说明为空或过长")

    def identifier(value):
        require(isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", value), "规则标识只能包含字母、数字、横线和下划线")

    def pattern(value):
        require(isinstance(value, str) and len(value) <= 4000, "匹配表达式过长")
        try:
            compiled(value)
        except re.error as exc:
            raise ValueError(f"匹配表达式无效：{exc}") from exc

    def steps(items):
        require(isinstance(items, list) and len(items) <= 30, "文本处理步骤必须是列表且不超过 30 项")
        for item in items:
            operation = item.get("op") if isinstance(item, dict) else None
            if operation in ("normalize", "compact"):
                keys(item, ("op",))
            elif operation == "translate":
                keys(item, ("op", "value"))
                require(isinstance(item["value"], dict) and len(item["value"]) <= 5000, "字符转换表无效")
                require(all(isinstance(k, str) and len(k) == 1 and isinstance(v, str) and len(v) <= 16
                            for k, v in item["value"].items()), "转换表的键必须是单个字符，值必须是短文本")
            elif operation == "replace":
                keys(item, ("op", "pattern", "value"))
                pattern(item["pattern"])
                require(isinstance(item["value"], str) and len(item["value"]) <= 4000, "替换内容无效")
                try:
                    transform("", [item])
                except (re.error, IndexError) as exc:
                    raise ValueError(f"替换内容无效：{exc}") from exc
            else:
                raise ValueError("不支持的文本处理步骤")

    keys(document, ("format", "views", "conditions", "stages"))
    require(document["format"] == 1, "不支持的规则文档格式")
    require(len(json.dumps(document, ensure_ascii=False)) <= 500_000, "规则文档超过 500 KB")
    indexed = {}
    for name in ("views", "conditions", "stages"):
        items = document[name]
        require(isinstance(items, list) and len(items) <= 300, "规则列表无效或超过 300 项")
        for item in items:
            require(isinstance(item, dict), "规则必须是对象")
            identifier(item.get("id"))
            label(item.get("title"))
        indexed[name] = {item["id"]: item for item in items}
        require(len(indexed[name]) == len(items), "同类规则的标识不能重复")
    fields = {"text", "source_titles", "direct", "$item", "$match", "$prefix", "$start"} | indexed["views"].keys()
    def field(value, local=(), text_only=False):
        require(isinstance(value, str) and value in fields, "引用的文本字段不存在")
        require(not value.startswith("$") or value in local, "匹配片段和当前分句只能在对应的附加条件内使用")
        if text_only:
            require(value not in ("source_titles", "direct", "$start"), "此条件需要文本字段")

    refs = {name: {identifier: set() for identifier in indexed[name]} for name in indexed}
    for item in document["views"]:
        keys(item, ("id", "title", "source", "steps"))
        field(item["source"], text_only=True)
        require(item["source"] not in {"direct", "source_titles", "$item", "$match", "$prefix", "$start"}, "文本视图必须来自正文或其他文本视图")
        if item["source"] in indexed["views"]:
            refs["views"][item["id"]].add(item["source"])
        steps(item["steps"])

    counter = 0
    def expression(item, edges, depth=0, local=()):
        nonlocal counter
        counter += 1
        require(depth <= 24 and counter <= 4000, "条件过多或嵌套过深")
        require(isinstance(item, dict), "条件必须是对象")
        operation = item.get("op")
        if operation == "always":
            keys(item, ("op",))
        elif operation in ("all", "any"):
            keys(item, ("op", "items"))
            require(isinstance(item["items"], list) and 1 <= len(item["items"]) <= 100, "组合条件至少需要一项，最多 100 项")
            for child in item["items"]:
                expression(child, edges, depth + 1, local)
        elif operation == "not":
            keys(item, ("op", "item"))
            expression(item["item"], edges, depth + 1, local)
        elif operation == "ref":
            keys(item, ("op", "ref"), ("input",))
            require(isinstance(item["ref"], str) and item["ref"] in indexed["conditions"], "引用的公共条件不存在")
            edges.add(item["ref"])
            if "input" in item:
                field(item["input"], local, text_only=True)
        elif operation in ("empty", "truthy", "equals"):
            keys(item, ("op", "input", "value") if operation == "equals" else ("op", "input"))
            field(item["input"], local)
            if operation == "equals":
                require(type(item["value"]) in (str, int, bool), "相等条件的比较值无效")
        elif operation == "each":
            keys(item, ("op", "input", "condition"), ("split", "steps"))
            field(item["input"], local)
            if "split" in item:
                field(item["input"], local, text_only=True)
                pattern(item["split"])
            else:
                require(item["input"] == "source_titles", "遍历文本必须指定分句表达式")
            steps(item.get("steps", []))
            expression(item["condition"], edges, depth + 1, {*local, "$item"})
        elif operation == "match":
            keys(item, ("op", "input", "pattern", "mode"), ("where", "prefix_chars"))
            field(item["input"], local, text_only=True)
            pattern(item["pattern"])
            require(item["mode"] in ("search", "match", "fullmatch", "find"), "不支持的匹配方式")
            if "prefix_chars" in item:
                require(type(item["prefix_chars"]) is int and 0 <= item["prefix_chars"] <= 1000, "匹配前文长度无效")
            if "where" in item:
                expression(item["where"], edges, depth + 1, {*local, "$match", "$prefix", "$start"})
        else:
            raise ValueError("不支持的条件类型")

    def enabled(item):
        require(type(item["enabled"]) is bool, "启用状态必须是布尔值")

    for item in document["conditions"]:
        keys(item, ("id", "title", "enabled", "condition"))
        enabled(item)
        expression(item["condition"], refs["conditions"][item["id"]])
    rule_ids = set()
    for stage in document["stages"]:
        keys(stage, ("id", "title", "enabled", "first_match", "before", "condition", "rules"))
        enabled(stage)
        require(type(stage["first_match"]) is bool, "命中顺序设置无效")
        require(isinstance(stage["before"], list) and all(isinstance(v, str) and v in indexed["stages"] for v in stage["before"]), "引用的前置规则组不存在")
        refs["stages"][stage["id"]].update(stage["before"])
        require(all(value.startswith("chat_") == stage["id"].startswith("chat_") for value in stage["before"]), "聊天排除规则组与成员处置规则组不能互相引用")
        expression(stage["condition"], set())
        require(isinstance(stage["rules"], list) and len(stage["rules"]) <= 1000, "规则组最多包含 1000 条规则")
        for rule in stage["rules"]:
            keys(rule, ("id", "title", "enabled", "action", "reason", "condition"), ("reason_field",))
            identifier(rule["id"])
            require(rule["id"] not in rule_ids, "规则标识不能重复")
            rule_ids.add(rule["id"])
            label(rule["title"])
            label(rule["reason"], 500)
            enabled(rule)
            require(rule["action"] in (("exclude",) if stage["id"].startswith("chat_") else ("delete", "mute", "ban")), "该规则组不支持所选处置")
            expression(rule["condition"], set())
            if rule.get("reason_field"):
                field(rule["reason_field"], text_only=True)
    for name, graph in refs.items():
        visited, active = set(), set()
        def visit(node):
            require(node not in active, f"{name} 存在循环引用")
            if node in visited:
                return
            active.add(node)
            for child in graph[node]:
                visit(child)
            active.remove(node)
            visited.add(node)
        for node in graph:
            visit(node)
    require({"s_norm", "s_auth", "a_auth"} <= indexed["views"].keys(), "缺少通用关键词所用的文本处理视图")
    require({"communication_context", "discussion", "contact_audience", "invites_audience"} <= indexed["conditions"].keys(), "缺少通用举报与讨论条件")
    require({"solicitation", "short", "profile", "advertising", "chat_solicitation", "chat_advertising"} <= indexed["stages"].keys(), "缺少必要的规则组")
    return copy.deepcopy(document)
