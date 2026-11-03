import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jsonschema
import pytest

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL / "scripts"))

import repo_health_checker as rhc  # noqa: E402
from repo_health_checker import (NotFound, RateLimited, check_all, classify, entries_from,  # noqa: E402
                                 first_response, guide_paths, is_outside_contribution, merge_stats,
                                 policy_notes, response_days)

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
LIMITS = argparse.Namespace(days=90, min_age_days=7, sample=15, min_sample=5, max_response_days=5,
                            max_merge_days=21, min_merge_rate=0.2, inactive_days=60, repo_field="full_name")
ALICE = {"login": "alice", "type": "User"}          # outside contributor
MAINTAINER = {"login": "maya", "type": "User"}
BOT = {"login": "helper[bot]", "type": "Bot"}


def ago(days: float) -> str:
    return (NOW - timedelta(days=days)).isoformat().replace("+00:00", "Z")


def issue(number=1, created_days_ago=20, user=ALICE, association="NONE", pr=None, state="open"):
    item = {"number": number, "created_at": ago(created_days_ago), "user": user,
            "author_association": association, "state": state}
    if pr is not None:
        item["pull_request"] = {"merged_at": pr}
    return item


def comment(days_ago, user=MAINTAINER, association="MEMBER"):
    return {"event": "commented", "user": user, "actor": user, "author_association": association,
            "created_at": ago(days_ago)}


def triage(kind, days_ago, actor=MAINTAINER):
    return {"event": kind, "actor": actor, "created_at": ago(days_ago)}


# ---- what counts as an answer ----

def test_maintainer_comment_is_the_first_response():
    timeline = [comment(18, ALICE, "NONE"), comment(17), comment(10)]
    assert first_response(issue(), timeline) == NOW - timedelta(days=17)


def test_review_counts_as_a_response():
    review = {"event": "reviewed", "user": MAINTAINER, "author_association": "OWNER", "submitted_at": ago(19)}
    assert first_response(issue(), [review]) == NOW - timedelta(days=19)


@pytest.mark.parametrize("kind", ["labeled", "assigned", "closed", "merged"])
def test_triage_by_someone_else_counts_as_a_response(kind):
    assert first_response(issue(), [triage(kind, 15)]) == NOW - timedelta(days=15)


def test_bots_and_the_author_do_not_count():
    timeline = [comment(19, BOT, "CONTRIBUTOR"), triage("labeled", 19, BOT), triage("closed", 18, ALICE)]
    assert first_response(issue(), timeline) is None


def test_comment_from_another_outside_user_does_not_count():
    assert first_response(issue(), [comment(19, {"login": "bob", "type": "User"}, "CONTRIBUTOR")]) is None


def test_events_that_show_no_reaction_are_ignored():
    assert first_response(issue(), [triage("subscribed", 19), triage("cross-referenced", 19)]) is None


@pytest.mark.parametrize("item, outside", [
    (issue(), True),
    (issue(association="CONTRIBUTOR"), True),
    (issue(user=MAINTAINER, association="MEMBER"), False),
    (issue(user=BOT, association="CONTRIBUTOR"), False),
])
def test_only_outside_contributions_are_measured(item, outside):
    assert is_outside_contribution(item) is outside


def test_unanswered_item_waits_its_whole_age():
    assert response_days(issue(created_days_ago=30), [], NOW) == (30.0, False)


def test_answered_item_waits_until_the_answer():
    assert response_days(issue(created_days_ago=30), [comment(28)], NOW) == (2.0, True)


# ---- merging ----

def test_merge_stats():
    prs = [issue(1, 30, pr=ago(28), state="closed"), issue(2, 30, pr=ago(20), state="closed"),
           issue(3, 30, pr=None, state="closed"), issue(4, 30, pr=None)]
    assert merge_stats(prs) == {"merged_prs": 2, "closed_unmerged_prs": 1, "open_prs": 1,
                                "median_merge_days": 6.0, "merge_rate": 0.67}


def test_merge_stats_without_prs():
    assert merge_stats([])["merge_rate"] is None


# ---- status ----

def repo(pushed_days_ago=1, archived=False):
    return {"pushed_at": ago(pushed_days_ago), "archived": archived, "stargazers_count": 1500}


def metrics(response=2.0, merge=7.0, merged=4, rejected=1, issues=10, prs=5, answered=12):
    return {"sample_issues": issues, "sample_prs": prs, "answered": answered,
            "median_first_response_days": response, "median_merge_days": merge, "merged_prs": merged,
            "closed_unmerged_prs": rejected, "merge_rate": merged / (merged + rejected) if merged + rejected else None}


def test_fast_repo_is_healthy():
    assert classify(repo(), metrics(), NOW, LIMITS)[0] == "healthy"


def test_slow_first_answer_is_unhealthy():
    status, reason = classify(repo(), metrics(response=9.0), NOW, LIMITS)
    assert status == "unhealthy" and "first answer takes 9.0 days" in reason


def test_slow_merge_is_unhealthy():
    assert classify(repo(), metrics(merge=40.0), NOW, LIMITS)[0] == "unhealthy"


def test_repo_that_closes_outside_prs_without_merging_is_unhealthy():
    status, reason = classify(repo(), metrics(response=0.1, merge=None, merged=0, rejected=15), NOW, LIMITS)
    assert status == "unhealthy" and "merged only 0 of 15" in reason


def test_few_closed_prs_say_nothing_about_the_merge_rate():
    assert classify(repo(), metrics(merge=None, merged=0, rejected=3), NOW, LIMITS)[0] == "healthy"


def test_archived_or_idle_repo_is_inactive():
    assert classify(repo(archived=True), metrics(), NOW, LIMITS)[0] == "inactive"
    assert classify(repo(pushed_days_ago=200), metrics(), NOW, LIMITS)[0] == "inactive"


def test_nobody_answering_is_inactive():
    assert classify(repo(), metrics(response=40.0, answered=0), NOW, LIMITS)[0] == "inactive"


def test_too_small_a_sample_is_unknown():
    assert classify(repo(), metrics(issues=2, prs=1), NOW, LIMITS)[0] == "unknown"


# ---- contributing guide ----

def test_guide_files_are_found_in_root_github_and_docs():
    listings = {
        "": [{"type": "file", "name": "README.md", "path": "README.md"},
             {"type": "dir", "name": "contributing", "path": "contributing"}],
        ".github": [{"type": "file", "name": "CONTRIBUTING.md", "path": ".github/CONTRIBUTING.md"},
                    {"type": "file", "name": "AI_POLICY.md", "path": ".github/AI_POLICY.md"}],
        "docs": [{"type": "file", "name": "contributing.rst", "path": "docs/contributing.rst"}],
    }
    assert [e["path"] for e in guide_paths(listings)] == [
        ".github/CONTRIBUTING.md", ".github/AI_POLICY.md", "docs/contributing.rst"]


def test_policy_notes_quote_the_lines_instead_of_guessing():
    notes = policy_notes({
        "CONTRIBUTING.md": "Run the tests.\nWe do not have a formal CLA.\nPRs written by ChatGPT are closed.",
        ".github/AI_POLICY.md": "Anything at all.",
    })
    assert notes["cla"] == ["We do not have a formal CLA."]
    assert notes["ai_policy"] == ["PRs written by ChatGPT are closed.", "has an AI policy: .github/AI_POLICY.md"]


def test_words_containing_ai_are_not_an_ai_policy():
    assert policy_notes({"CONTRIBUTING.md": "Please explain the main change and maintain the style."})["ai_policy"] == []


# ---- whole run against a fake GitHub ----

class FakeGitHub:
    def __init__(self, routes: dict):
        self.routes = routes
        self.calls = []

    def __call__(self, path: str):
        self.calls.append(path)
        for prefix, answer in self.routes.items():
            if path.startswith(prefix):
                if isinstance(answer, Exception):
                    raise answer
                return answer
        raise NotFound(path)


def healthy_repo_routes(name="acme/widgets"):
    issues = [issue(n, 10 + n) for n in range(1, 6)]
    prs = [issue(n, 10 + n, pr=ago(8 + n), state="closed") for n in range(6, 11)]
    routes = {f"/repos/{name}/issues/{n}/timeline": [comment(9 + n)] for n in range(1, 11)}
    routes.update({
        f"/repos/{name}/issues?": issues + prs + [issue(99, 2)],     # 99 is too young to judge
        f"/repos/{name}/contents/.github": [{"type": "file", "name": "CONTRIBUTING.md",
                                             "path": ".github/CONTRIBUTING.md", "download_url": "https://raw/c"}],
        f"/repos/{name}": repo(),
    })
    return routes


@pytest.fixture
def no_downloads(monkeypatch):
    monkeypatch.setattr(rhc, "get_text", lambda url: "Fork, branch, test, open a PR.")


def test_whole_run_matches_the_schema(no_downloads):
    get = FakeGitHub(healthy_repo_routes())
    result = check_all(["acme/widgets", {"full_name": "acme/missing", "stars": 2000}], get, NOW, LIMITS)

    schema = json.loads((SKILL / "schema.json").read_text(encoding="utf-8"))["output"]
    jsonschema.validate(result, schema)
    [entry] = result["healthy"]
    assert entry["full_name"] == "acme/widgets"
    assert entry["health"]["metrics"]["sample_issues"] == 5
    assert entry["health"]["metrics"]["median_first_response_days"] == 1.0
    assert entry["health"]["contributing"]["files"] == [".github/CONTRIBUTING.md"]
    assert "/repos/acme/widgets/issues/99/timeline?per_page=100" not in get.calls
    [missing] = result["unknown"]
    assert missing["stars"] == 2000 and missing["health"]["reason"] == "repository not found"


def test_rate_limit_stops_the_run_but_keeps_every_repo(no_downloads):
    get = FakeGitHub({"/repos/": RateLimited("GitHub rate limit reached, resets at 13:00")})
    result = check_all(["a/one", "b/two"], get, NOW, LIMITS)
    assert [e["full_name"] for e in result["unknown"]] == ["a/one", "b/two"]
    assert len(get.calls) == 1


def test_accepts_rule_filter_output():
    assert entries_from({"kept": [{"full_name": "a/b"}], "rejected": []}) == [{"full_name": "a/b"}]
