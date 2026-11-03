import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import jsonschema
import pytest

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL / "scripts"))

from github_issue_lister import (NotFound, RateLimited, clean_body, is_available, list_all,  # noqa: E402
                                 newcomer_labels, repos_from)

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)


def options(**changes):
    values = dict(help_wanted=False, label=[], per_repo=10, max_age_days=365)
    return argparse.Namespace(**{**values, **changes})


# ---- labels ----

@pytest.mark.parametrize("name", [
    "good first issue", "Good First Issue", "good-first-issue", "good first issue for humans",
    "good first bug", "first-timers-only", "beginner", "beginner friendly", "easy", "E-easy",
    "difficulty: easy", "level/easy", "starter", "low-hanging-fruit", "good for newcomers",
])
def test_beginner_labels_are_recognized(name):
    assert newcomer_labels([name], help_wanted=False, extra=[]) == [name]


@pytest.mark.parametrize("name", [
    "I-lang-easy-decision", "D-newcomer-roadblock", "bug", "hacktoberfest", "not-easy", "needs triage",
])
def test_labels_that_only_look_like_beginner_labels_are_not(name):
    assert newcomer_labels([name], help_wanted=False, extra=[]) == []


def test_easy_must_be_a_whole_word():
    assert newcomer_labels(["easy"], False, []) == ["easy"]
    assert newcomer_labels(["easyconfig"], False, []) == []


def test_help_wanted_only_when_asked():
    names = ["help wanted", "help needed", "E-help-wanted"]
    assert newcomer_labels(names, help_wanted=False, extra=[]) == []
    assert newcomer_labels(names, help_wanted=True, extra=[]) == names


def test_extra_labels_are_matched_by_exact_name():
    assert newcomer_labels(["contrib welcome", "welcome"], False, ["Contrib Welcome"]) == ["contrib welcome"]


# ---- issues ----

def test_template_comments_are_removed_from_the_body():
    body = "<!-- Please describe the bug.\nThanks! -->\nThe button does nothing.\n\n\n\n<!-- version -->2.1"
    assert clean_body(body) == "The button does nothing.\n\n2.1"
    assert clean_body(None) == ""


@pytest.mark.parametrize("item, available", [
    ({}, True),
    ({"pull_request": {}}, False),
    ({"assignees": [{"login": "bob"}]}, False),
    ({"locked": True}, False),
])
def test_pull_requests_assigned_and_locked_issues_are_not_available(item, available):
    assert is_available(item) is available


# ---- input ----

def test_takes_healthy_repos_from_the_health_checker_by_default():
    data = {"healthy": [{"full_name": "a/one", "language": "Java", "stars": 2000, "health": {}}],
            "unhealthy": [{"full_name": "b/two"}], "inactive": [], "unknown": [{"full_name": "c/three"}]}
    assert [r["full_name"] for r in repos_from(data, ["healthy"])] == ["a/one"]
    assert [r["full_name"] for r in repos_from(data, ["healthy", "unknown"])] == ["a/one", "c/three"]
    assert repos_from(data, ["healthy"])[0] == {"full_name": "a/one", "language": "Java", "stars": 2000}


@pytest.mark.parametrize("data", [["a/one"], [{"full_name": "a/one"}], {"repos": [{"full_name": "a/one"}]},
                                  {"kept": [{"full_name": "a/one"}], "rejected": []}])
def test_other_inputs(data):
    assert [r["full_name"] for r in repos_from(data, ["healthy"])] == ["a/one"]


# ---- whole run against a fake GitHub ----

def issue(number, labels, created="2026-10-01T10:00:00Z", **extra):
    return {"number": number, "html_url": f"https://github.com/acme/widgets/issues/{number}",
            "title": f"Issue {number}", "labels": [{"name": n} for n in labels], "body": "Fix it.",
            "comments": 1, "author_association": "MEMBER", "created_at": created,
            "updated_at": created, "assignees": [], **extra}


class FakeGitHub:
    def __init__(self, routes: dict):
        self.routes, self.calls = routes, []

    def __call__(self, path: str):
        self.calls.append(path)
        for prefix, answer in self.routes.items():
            if path.startswith(prefix):
                if isinstance(answer, Exception):
                    raise answer
                return answer
        raise NotFound(path)


def widgets_routes():
    labels = [{"name": "bug"}, {"name": "good first issue"}, {"name": "E-easy"}]
    gfi = [issue(1, ["good first issue", "bug"], "2026-10-20T10:00:00Z"),
           issue(2, ["good first issue", "E-easy"], "2026-09-01T10:00:00Z"),
           issue(3, ["good first issue"], "2019-05-01T10:00:00Z"),          # too old
           issue(4, ["good first issue"], pull_request={})]
    easy = [issue(2, ["good first issue", "E-easy"], "2026-09-01T10:00:00Z"),  # same issue again
            issue(5, ["E-easy"], "2026-10-25T10:00:00Z")]
    return {
        "/repos/acme/widgets/labels": labels,
        "/repos/acme/widgets/issues?labels=good+first+issue": gfi,
        "/repos/acme/widgets/issues?labels=E-easy": easy,
    }


def test_whole_run_matches_the_schema():
    get = FakeGitHub(widgets_routes())
    result = list_all([{"full_name": "acme/widgets", "language": "Python", "stars": 1500},
                       {"full_name": "acme/gone"}], get, options(), NOW)

    schema = json.loads((SKILL / "schema.json").read_text(encoding="utf-8"))["output"]
    jsonschema.validate(result, schema)
    assert [i["key"] for i in result["issues"]] == ["acme/widgets#5", "acme/widgets#1", "acme/widgets#2"]
    first = result["issues"][1]
    assert first["labels"] == ["good first issue", "bug"] and first["newcomer_labels"] == ["good first issue"]
    assert first["repo_language"] == "Python" and first["repo_stars"] == 1500
    assert result["repos"] == [
        {"full_name": "acme/widgets", "labels": ["good first issue", "E-easy"], "issues": 3, "error": None},
        {"full_name": "acme/gone", "labels": [], "issues": 0, "error": "repository not found"},
    ]
    assert all("assignee=none" in c and "state=open" in c for c in get.calls if "/issues?" in c)


def test_keeps_only_the_newest_per_repo():
    result = list_all([{"full_name": "acme/widgets"}], FakeGitHub(widgets_routes()), options(per_repo=1), NOW)
    assert [i["key"] for i in result["issues"]] == ["acme/widgets#5"]


def test_repo_without_newcomer_labels_lists_nothing():
    get = FakeGitHub({"/repos/acme/plain/labels": [{"name": "bug"}]})
    result = list_all([{"full_name": "acme/plain"}], get, options(), NOW)
    assert result["issues"] == [] and len(get.calls) == 1


def test_rate_limit_stops_the_run_but_reports_every_repo():
    get = FakeGitHub({"/repos/": RateLimited("GitHub rate limit reached")})
    result = list_all([{"full_name": "a/one"}, {"full_name": "b/two"}], get, options(), NOW)
    assert [r["error"] for r in result["repos"]] == ["GitHub rate limit reached"] * 2
    assert len(get.calls) == 1
