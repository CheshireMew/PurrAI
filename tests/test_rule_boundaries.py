"""Make new embedded keywords a failing check instead of a review promise."""

import importlib.util
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("rule_boundaries_check", ROOT / "scripts/check_rule_boundaries.py")
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)


def state(source):
    return {"entries": guard.findings("src/purrai/new_filter.py", source),
            "adapter_ast": {path: "unchanged" for path in guard.ADAPTERS}, "seed_sha256": "unchanged"}


@pytest.mark.parametrize("source", [
    "def blocked(text):\n    return '测试推广词' in text\n",
    "def blocked(name):\n    return name.startswith('测试营销名')\n",
    "def blocked(body):\n    return body == 'pump'\n",
    "import re\ndef blocked(text):\n    return re.search('示例词|另一个词', text)\n",
    "import re\nAD = re.compile(r'示例[推廣广]词')\n",
    "BLOCKED_WORDS = ['example-promotion']\n",
    "HIDDEN = {'新广告词', '另一个词'}\n",
    "NEW_TERM = '新广告词'\n",
    "new_terms = {'value': '新推广词'}\n",
    "def blocked(text):\n    return ('测' + '试') in text\n",
    "def blocked(text, sep):\n    return f'测{sep}试' in text\n",
    "def blocked(text):\n    return matches(text, '新推广词', 'contains')\n",
])
def test_new_keyword_branches_regexes_and_word_tables_are_rejected(source):
    baseline = state("")
    errors = guard.differences(state(source), baseline)
    assert errors and "new_filter.py" in errors[0]


@pytest.mark.parametrize("source", [
    "def blocked(text, rule):\n    return rule['value'] in text\n",
    "import re\ndef compile_rule(rule):\n    return re.compile(rule['value'])\n",
    "def result():\n    return {'detail': '普通错误提示'}\n",
    "def progress(row):\n    row.state, row.error = 'failed', '普通错误提示'\n",
    "import re\ndef normalize(text):\n    return re.sub(r'(?<=[\\u4e00-\\u9fff])[a-z0-9]+(?=[\\u4e00-\\u9fff])', '', text)\n",
])
def test_database_rule_values_and_generic_character_handling_are_allowed(source):
    assert not guard.findings("src/purrai/engine.py", source)


def test_copying_a_legacy_expression_does_not_create_an_extra_exemption():
    one = "def blocked(text):\n    if '示例推广词' in text:\n        return True\n"
    two = one + "    if '示例推广词' in text:\n        return True\n"
    assert guard.differences(state(two), state(one))


def test_line_movement_does_not_require_refreshing_the_baseline():
    source = "def blocked(text):\n    return '示例推广词' in text\n"
    assert not guard.differences(state("# formatting only\n\n" + source), state(source))


def test_reintroducing_logic_into_migrated_adapters_is_reported():
    before, after = state(""), state("")
    after["adapter_ast"][guard.ADAPTERS[0]] = "changed"
    assert any("冻结" in error for error in guard.differences(after, before))


def test_missing_migrated_adapter_is_not_silently_ignored():
    before, after = state(""), state("")
    after["adapter_ast"].pop(guard.ADAPTERS[0])
    assert any("缺失" in error for error in guard.differences(after, before))


def test_modifying_the_installation_seed_is_not_a_backdoor_for_new_cases():
    before, after = state(""), state("")
    after["seed_sha256"] = "changed"
    assert any("安装种子" in error for error in guard.differences(after, before))


def test_cli_fails_closed_when_the_baseline_cannot_be_read(monkeypatch, capsys):
    def missing(_):
        raise FileNotFoundError("baseline missing")
    monkeypatch.setattr(guard, "check", missing)
    monkeypatch.setattr(sys, "argv", ["check_rule_boundaries.py"])
    assert guard.main() == 1
    assert "检查失败" in capsys.readouterr().out


def test_cli_does_not_offer_an_automatic_baseline_refresh(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["check_rule_boundaries.py", "--write"])
    with pytest.raises(SystemExit) as error:
        guard.main()
    assert error.value.code == 2


def test_refreshed_baseline_cannot_silently_accept_new_keywords():
    raw = (ROOT / guard.BASELINE).read_bytes()
    assert guard.read_baseline(raw)["format"] == 2
    with pytest.raises(ValueError, match="冻结基线已被改动"):
        guard.read_baseline(raw + b" ")


def test_repository_rule_boundaries():
    assert not (errors := guard.check(ROOT)), "\n".join(errors)
