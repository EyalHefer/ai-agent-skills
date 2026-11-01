import json
import subprocess
import sys
from pathlib import Path

import jsonschema

SKILL = Path(__file__).resolve().parent.parent
SCRIPT = SKILL / "scripts" / "rule_filter.py"
sys.path.insert(0, str(SCRIPT.parent))

from rule_filter import apply_rules, build_rules  # noqa: E402


def run(items, *exclude, fields=("title",)):
    rules = build_rules({"rules": [{"name": "test", "fields": list(fields), "exclude": list(exclude)}]})
    return apply_rules(items, rules)


def kept_titles(items, *exclude):
    return [i["title"] for i in run(items, *exclude)["kept"]]


def test_matches_whole_words_only():
    items = [{"title": "Team Lead"}, {"title": "Leading Backend Developer"}]
    assert kept_titles(items, "lead") == ["Leading Backend Developer"]


def test_devops_is_not_rejected_by_operations():
    items = [{"title": "DevOps Engineer"}, {"title": "Operations Analyst"}]
    assert kept_titles(items, "operations") == ["DevOps Engineer"]


def test_is_case_insensitive():
    assert kept_titles([{"title": "SENIOR Developer"}], "senior") == []


def test_word_with_punctuation():
    assert kept_titles([{"title": "Sr. Developer"}], "sr.") == []


def test_trailing_wildcard():
    assert kept_titles([{"title": "Technical Recruiter"}], "recruit*") == []


def test_leading_wildcard_for_hebrew_prefix():
    assert kept_titles([{"title": "מנהל לשיווק דיגיטלי"}], "*שיווק") == []


def test_hebrew_whole_word_is_not_found_inside_another_word():
    assert kept_titles([{"title": "מחשמלים"}], "חשמל") == ["מחשמלים"]


def test_rejection_reports_rule_and_match():
    result = run([{"title": "Senior Developer"}], "senior")
    assert result["rejected"][0]["rule"] == "test"
    assert result["rejected"][0]["matched"] == "senior"


def test_list_field_is_searched():
    result = run([{"labels": ["good first issue", "wontfix"]}], "wontfix", fields=["labels"])
    assert result["kept"] == []


def test_missing_field_and_empty_rule_keep_the_item():
    assert run([{"title": "Developer"}], "x", fields=["location"])["kept"] == [{"title": "Developer"}]
    no_words = build_rules({"rules": [{"name": "n", "fields": ["title"], "exclude": []}]})
    assert apply_rules([{"title": "a"}], no_words)["kept"] == [{"title": "a"}]


def test_cli_output_matches_schema_and_ignores_other_settings(tmp_path):
    rules_file = tmp_path / "preferences.yaml"
    rules_file.write_text(
        "target_roles: [backend]\nrules:\n  - name: level\n    fields: [title]\n    exclude: [senior]\n",
        encoding="utf-8")
    items = [{"title": "Senior מפתח"}, {"title": "Junior מפתח"}]
    result = subprocess.run([sys.executable, str(SCRIPT), str(rules_file)], check=True, capture_output=True,
                            input=json.dumps(items, ensure_ascii=False).encode("utf-8"))
    output = json.loads(result.stdout.decode("utf-8"))
    schema = json.loads((SKILL / "schema.json").read_text(encoding="utf-8"))
    jsonschema.validate(output, schema["output"])
    assert output["kept"] == [{"title": "Junior מפתח"}]
