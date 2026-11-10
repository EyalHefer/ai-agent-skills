import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import jsonschema
import pytest

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


# ---- number and date rules ----

NOW = datetime(2026, 11, 3, tzinfo=timezone.utc)


def numbers_rule(**condition):
    return build_rules({"rules": [{"name": "busy", "fields": ["comments"], **condition}]})


def test_number_above():
    items = [{"comments": 25}, {"comments": 20}, {"comments": "31"}, {}]
    result = apply_rules(items, numbers_rule(above=20), NOW)
    assert result["kept"] == [{"comments": 20}, {}]
    assert [r["matched"] for r in result["rejected"]] == ["comments 25 > 20", "comments 31 > 20"]


def test_number_below():
    result = apply_rules([{"comments": 0}, {"comments": 3}], numbers_rule(below=1), NOW)
    assert result["kept"] == [{"comments": 3}]


def test_text_and_booleans_are_not_numbers():
    assert apply_rules([{"comments": "many"}, {"comments": True}], numbers_rule(above=0), NOW)["rejected"] == []


def test_dates():
    rules = build_rules({"rules": [{"name": "stale", "fields": ["updated_at"], "older_than_days": 180}]})
    items = [{"updated_at": "2026-01-01T00:00:00Z"}, {"updated_at": "2026-10-01T00:00:00Z"}, {"updated_at": "soon"}]
    result = apply_rules(items, rules, NOW)
    assert [r["item"] for r in result["rejected"]] == [items[0]]
    assert result["rejected"][0]["matched"] == "updated_at 306 days ago, more than 180"


def test_newer_than():
    rules = build_rules({"rules": [{"name": "too new", "fields": ["created_at"], "newer_than_days": 7}]})
    result = apply_rules([{"created_at": "2026-11-01"}, {"created_at": "2026-10-01"}], rules, NOW)
    assert result["kept"] == [{"created_at": "2026-10-01"}]


def test_rules_without_conditions_are_ignored_and_unknown_keys_fail():
    assert build_rules({"rules": [{"name": "empty", "fields": ["title"], "exclude": []}]}) == []
    with pytest.raises(ValueError, match="unknown keys"):
        build_rules({"rules": [{"name": "typo", "fields": ["comments"], "more_than": 3}]})


def test_rules_file_with_numbers_matches_the_schema():
    config = {"rules": [{"name": "busy", "fields": ["comments"], "above": 20},
                        {"name": "stale", "fields": ["updated_at"], "older_than_days": 180},
                        {"name": "words", "fields": ["title"], "exclude": ["epic"]}]}
    schema = json.loads((SKILL / "schema.json").read_text(encoding="utf-8"))["rules_file"]
    jsonschema.validate(config, schema)
