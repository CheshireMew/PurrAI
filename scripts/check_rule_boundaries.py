"""Detect new embedded matching content without executing application code."""

import argparse
import ast
from collections import Counter
import hashlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
BASELINE = "docs/rule_boundaries_baseline.json"
BASELINE_SHA256 = "1ce063356cf83f4d1a448f2dfba75120a18b47b1acda3910d95be3c175ba3d79"
ADAPTERS = (
    "src/purrai/solicitation_rules.py",
    "src/purrai/advertising_patterns.py",
    "src/purrai/chat_input.py",
)
SEED = "src/purrai/assets/business_rules.v1.json"
TEXT_NAMES = re.compile(r"(?:^|_)(?:text|body|content|caption|name|title|profile|haystack)(?:$|_)")
POLICY_NAMES = re.compile(r"keyword|phrase|term|pattern|words|blacklist|whitelist|blocked|forbidden", re.I)
MATCH_CALLS = {"compile", "search", "match", "fullmatch", "findall", "finditer", "sub", "subn"}
STRING_CALLS = {"startswith", "endswith", "find", "count", "index", "replace"}


def digest(node):
    return hashlib.sha256(ast.dump(node, include_attributes=False).encode()).hexdigest()


def strings(node):
    # Inspect literal data, not field names in rule['value'] or result labels
    # reached through arbitrary function calls.
    if isinstance(node, ast.Constant):
        return [node.value] if isinstance(node.value, str) else []
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return [value for item in node.elts for value in strings(item)]
    if isinstance(node, ast.Dict):
        return [value for item in [*node.keys, *node.values] if item is not None for value in strings(item)]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        parts = strings(node.left) + strings(node.right)
        return parts + ["".join(parts)]
    if isinstance(node, ast.JoinedStr):
        parts = [value for item in node.values if isinstance(item, ast.Constant) for value in strings(item)]
        return parts + ["".join(parts)]
    return []


def content(value, *, english=False):
    if len(re.findall(r"[\u3400-\u9fff]", value)) >= 2:
        return True
    if english:
        # Ignore generic escaped character classes and regex group names.
        value = re.sub(r"\[(?:\\.|[^\]\\])*\]|\\[A-Za-z0-9]+|\(\?P<[^>]+>", "", value)
        return bool(re.search(r"[A-Za-z]{3,}", value))
    return False


def text_context(node):
    return any(TEXT_NAMES.search(item.id) for item in ast.walk(node) if isinstance(item, ast.Name))


def findings(path, source):
    """Report expression identity and scope; line movement alone is harmless."""
    tree = ast.parse(source, filename=path)
    result = []

    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self.scope = []

        def record(self, node, kind, values):
            if values:
                result.append({"path": path, "scope": ".".join(self.scope) or "<module>",
                    "kind": kind, "digest": digest(node), "line": node.lineno,
                    "examples": sorted(set(values))[:5]})

        def visit_FunctionDef(self, node):
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_ClassDef(self, node):
            self.scope.append(node.name)
            self.generic_visit(node)
            self.scope.pop()

        def visit_Compare(self, node):
            values = [value for item in [node.left, *node.comparators] for value in strings(item)]
            self.record(node, "comparison", [value for value in values
                                              if content(value, english=text_context(node))])
            self.generic_visit(node)

        def visit_Call(self, node):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id if isinstance(node.func, ast.Name) else ""
            if name in MATCH_CALLS and node.args:
                self.record(node, "matching-call", [value for value in strings(node.args[0]) if content(value, english=True)])
            elif name in STRING_CALLS:
                self.record(node, "string-match", [value for arg in node.args for value in strings(arg)
                                                   if content(value, english=text_context(node))])
            elif name == "matches" and len(node.args) > 1:
                self.record(node, "keyword-call", [value for value in strings(node.args[1]) if content(value, english=True)])
            self.generic_visit(node)

        def visit_Assign(self, node):
            names = [item.id for target in node.targets for item in ast.walk(target) if isinstance(item, ast.Name)]
            if not any(isinstance(target, (ast.Tuple, ast.List)) for target in node.targets):
                self.assignment(node, node.value, names)
            self.generic_visit(node)

        def visit_AnnAssign(self, node):
            names = [item.id for item in ast.walk(node.target) if isinstance(item, ast.Name)]
            if node.value:
                self.assignment(node, node.value, names)
            self.generic_visit(node)

        def assignment(self, node, value, names):
            collection = isinstance(value, (ast.List, ast.Tuple, ast.Set, ast.Dict))
            policy_name = any(POLICY_NAMES.search(name) for name in names)
            constant = any(name.isupper() for name in names)
            if collection or constant or policy_name:
                self.record(node, "matching-data", [item for item in strings(value) if content(item, english=policy_name)])

    Visitor().visit(tree)
    return result


def inventory(root):
    records, adapters = [], {}
    for path in sorted((root / "src" / "purrai").rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        source = path.read_text(encoding="utf-8-sig")
        records.extend(findings(relative, source))
        if relative in ADAPTERS:
            adapters[relative] = digest(ast.parse(source))
    return {"format": 2, "adapter_ast": adapters, "entries": records,
            "seed_sha256": hashlib.sha256((root / SEED).read_bytes()).hexdigest()}


def identity(item):
    return item["path"], item["scope"], item["kind"], item["digest"]


def differences(current, baseline):
    remaining = Counter(identity(item) for item in baseline["entries"])
    errors = []
    for item in current["entries"]:
        key = identity(item)
        if remaining[key]:
            remaining[key] -= 1
        else:
            errors.append(f"{item['path']}:{item['line']} {item['scope']}：新增或改动的匹配内容 {item['examples']!r}")
    for path in ADAPTERS:
        if path not in current["adapter_ast"] or path not in baseline["adapter_ast"]:
            errors.append(f"{path}：迁移后的规则适配入口或检查记录缺失")
        elif current["adapter_ast"][path] != baseline["adapter_ast"][path]:
            errors.append(f"{path}：冻结的规则适配入口发生变化，须核实通用能力改动，不得重新嵌入业务样例")
    if current.get("seed_sha256") != baseline.get("seed_sha256"):
        errors.append(f"{SEED}：安装种子发生变化；日常业务词应通过后台维护，不得修改种子绕过管理入口")
    return errors


def read_baseline(raw):
    if hashlib.sha256(raw).hexdigest() != BASELINE_SHA256:
        raise ValueError("冻结基线已被改动；禁止通过刷新清单放行新业务词，须核实明确授权的迁移或结构审查")
    baseline = json.loads(raw.decode("utf-8"))
    if baseline.get("format") != 2:
        raise ValueError("不支持的规则边界基线格式")
    return baseline


def check(root=ROOT):
    baseline = read_baseline((root / BASELINE).read_bytes())
    return differences(inventory(root), baseline)


def main():
    parser = argparse.ArgumentParser(description="检查规则内容是否又被嵌入生产判断；不提供自动刷新基线")
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    try:
        errors = check(args.root.resolve())
    except (OSError, ValueError, SyntaxError) as exc:
        print(f"规则边界检查失败：{exc}")
        return 1
    if errors:
        print("规则边界检查失败。优先使用 Rule / SharedPolicy；禁止为当前样例刷新基线放行。")
        print("\n".join(errors))
        return 1
    print("规则边界检查通过：未新增硬编码匹配内容，三份文件仅保留规则适配，安装种子未变。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
