import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jsonschema
import pytest

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL / "scripts"))

from issue_reader import (CLAIM, NotFound, RateLimited, claims, classify, entries_from,  # noqa: E402
                          linked_prs, locate, pr_notes, read_all, thread_text)

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
REPO = "acme/widgets"
ALICE = {"login": "alice", "type": "User"}
BOB = {"login": "bob", "type": "User"}
MAYA = {"login": "maya", "type": "User"}       # maintainer
BOT = {"login": "stale[bot]", "type": "Bot"}


def ago(days: float) -> str:
    return (NOW - timedelta(days=days)).isoformat().replace("+00:00", "Z")


def comment(user, body, days_ago=5, association="NONE"):
    return {"event": "commented", "user": user, "actor": user, "author_association": association,
            "body": body, "created_at": ago(days_ago)}


def mention(number, state="open", merged=False, repo=REPO, author=BOB, days_ago=3, is_pr=True):
    source = {"number": number, "state": state, "user": author, "repository": {"full_name": repo},
              "html_url": f"https://github.com/{repo}/pull/{number}"}
    if is_pr:
        source["pull_request"] = {"merged_at": ago(1) if merged else None}
    return {"event": "cross-referenced", "created_at": ago(days_ago), "source": {"type": "issue", "issue": source}}


def pr(number, state="open", closes=True, author="bob"):
    return {"number": number, "url": "", "author": author, "state": state, "closes": closes, "at": ago(3)}


OPEN_ISSUE = {"state": "open", "assignees": []}


# ---- claims ----

@pytest.mark.parametrize("text", [
    "Hi! I'd like to work on this as my first contribution.",
    "Can I work on this issue?",
    "could I take this one?",
    "I'll take it",
    "I will work on this today",
    "I'm working on this",
    "Please assign this to me",
    "assign me please",
    "/assign",
    "May I be assigned?",
])
def test_claims_are_recognized(text):
    assert CLAIM.search(text)


@pytest.mark.parametrize("text", [
    "This would be easy to fix in parser.py.",
    "I think the work on this module is done elsewhere.",
    "Can you share the stack trace?",
    "I tried this and it fails on Windows too.",
])
def test_ordinary_comments_are_not_claims(text):
    assert not CLAIM.search(text)


def test_maintainers_and_bots_do_not_claim():
    timeline = [comment(MAYA, "I'll take this", association="MEMBER"), comment(BOT, "/assign")]
    assert claims(timeline) == []


def test_a_claim_given_back_is_dropped():
    timeline = [comment(ALICE, "Can I work on this?", 20), comment(ALICE, "Sorry, I won't have time.", 10),
                comment(BOB, "I'd like to take it", 5)]
    assert [c["author"] for c in claims(timeline)] == ["bob"]


# ---- linked pull requests ----

def test_only_pull_requests_in_the_same_repository_count():
    timeline = [mention(1), mention(2, repo="someone/fork"), mention(3, is_pr=False)]
    assert [p["number"] for p in linked_prs(timeline, REPO, closing={1})] == [1]


def test_pr_state_and_whether_it_closes_the_issue():
    timeline = [mention(1, state="closed", merged=True), mention(2, state="closed"), mention(3)]
    prs = linked_prs(timeline, REPO, closing={3})
    assert [(p["number"], p["state"], p["closes"]) for p in prs] == [
        (1, "merged", False), (2, "closed", False), (3, "open", True)]


# ---- status ----

def test_closed_issue():
    assert classify({"state": "closed"}, [], [], NOW, 30)[0] == "closed"


def test_assigned_issue_is_taken():
    status, reason, _ = classify({"state": "open", "assignees": [{"login": "bob"}]}, [], [], NOW, 30)
    assert (status, reason) == ("taken", "assigned to bob")


def test_open_pr_that_closes_it_is_taken_and_every_such_pr_is_named():
    status, reason, _ = classify(OPEN_ISSUE, [pr(7, author="you"), pr(9)], [], NOW, 30)
    assert (status, reason) == ("taken", "open PR #7 by you, #9 by bob")


def test_open_pr_that_only_mentions_it_does_not_take_it():
    status, _, notes = classify(OPEN_ISSUE, [pr(7, closes=False)], [], NOW, 30)
    assert status == "available"
    assert notes == ["open PRs that mention it but will not close it: #7"]


def test_recent_claim_takes_it_and_an_old_one_has_lapsed():
    recent = [{"author": "alice", "at": ago(4), "comment": ""}]
    old = [{"author": "alice", "at": ago(90), "comment": ""}]
    assert classify(OPEN_ISSUE, [], recent, NOW, 30)[:2] == ("taken", "alice asked for it 4 days ago")
    status, _, notes = classify(OPEN_ISSUE, [], old, NOW, 30)
    assert status == "available" and notes == ["alice asked for it 90 days ago, with no PR since"]


def test_merged_pr_that_closes_it_while_it_is_still_open_is_unknown():
    assert classify(OPEN_ISSUE, [pr(7, state="merged")], [], NOW, 30)[0] == "unknown"


def test_many_mentions_become_one_note_per_kind():
    prs = [pr(n, state="merged", closes=False) for n in range(1, 34)] + [pr(50, state="closed", closes=False)]
    assert pr_notes(prs) == ["33 merged PRs mention it, so it may be split into parts: #1, #2, #3 and 30 more",
                             "earlier PRs closed without merging: #50"]


# ---- text and input ----

def test_thread_text_has_the_issue_and_human_comments_in_order():
    issue = {"title": "Button does nothing", "body": "Click it."}
    timeline = [comment(ALICE, "Same here", 3), comment(BOT, "This issue is stale"),
                comment(MAYA, "Good first issue!", 2, "MEMBER")]
    text = thread_text(issue, timeline)
    assert text.startswith("Button does nothing\n\nClick it.")
    assert "--- alice, " in text and "--- maya (maintainer), " in text and "stale" not in text


@pytest.mark.parametrize("entry, where", [
    ({"url": "https://github.com/acme/widgets/issues/12"}, ("acme/widgets", 12)),
    ({"key": "acme/widgets#12"}, ("acme/widgets", 12)),
    ({"item": {"key": "acme/widgets#12", "url": "https://github.com/acme/widgets/issues/12"}}, ("acme/widgets", 12)),
    ({"url": "https://jobs.example.com/12"}, None),
])
def test_locate(entry, where):
    assert locate(entry) == where


@pytest.mark.parametrize("data", [[{"key": "a/b#1"}], {"issues": [{"key": "a/b#1"}], "repos": []},
                                  {"kept": [{"key": "a/b#1"}]}, {"scored": [{"key": "a/b#1"}], "unscored": []}])
def test_accepts_lister_filter_and_matcher_output(data):
    assert entries_from(data) == [{"key": "a/b#1"}]


# ---- whole run against a fake GitHub ----

class FakeGitHub:
    def __init__(self, routes: dict, closing: dict[int, set[int]]):
        self.routes, self.closing, self.calls = routes, closing, []

    def get(self, path: str):
        self.calls.append(path)
        for prefix, answer in self.routes.items():
            if path.startswith(prefix):
                if isinstance(answer, Exception):
                    raise answer
                return answer
        raise NotFound(path)

    def graphql(self, query: str, variables: dict) -> dict:
        nodes = [{"number": n} for n in self.closing.get(variables["number"], set())]
        return {"repository": {"issue": {"closedByPullRequestsReferences": {"nodes": nodes}}}}


def fake_widgets():
    return FakeGitHub({
        f"/repos/{REPO}/issues/1/timeline": [comment(ALICE, "Can I work on this?", 40), mention(10, days_ago=30)],
        f"/repos/{REPO}/issues/1": {"title": "Taken", "body": "", "state": "open", "assignees": [], "comments": 1},
        f"/repos/{REPO}/issues/2/timeline": [comment(MAYA, "Happy to review a PR", 2, "OWNER")],
        f"/repos/{REPO}/issues/2": {"title": "Free", "body": "Do it.", "state": "open", "assignees": [], "comments": 1},
    }, closing={1: {10}})


def test_whole_run_matches_the_schema():
    entries = [{"key": f"{REPO}#1", "title": "Taken"}, {"key": f"{REPO}#2", "title": "Free"},
               {"key": f"{REPO}#3"}, {"title": "no link"}]
    github = fake_widgets()
    result = read_all(entries, github.get, github.graphql, NOW, 30)

    schema = json.loads((SKILL / "schema.json").read_text(encoding="utf-8"))["output"]
    jsonschema.validate(result, schema)
    [taken] = result["taken"]
    assert taken["title"] == "Taken" and taken["thread"]["reason"] == "open PR #10 by bob"
    [free] = result["available"]
    assert free["thread"]["last_maintainer_comment_at"] == ago(2)
    assert "Happy to review a PR" in free["thread"]["text"]
    assert [e["thread"]["reason"] for e in result["unknown"]] == ["issue not found", "no GitHub issue url or key"]


def test_rate_limit_stops_the_run_but_keeps_every_issue():
    github = FakeGitHub({"/repos/": RateLimited("GitHub rate limit reached")}, {})
    result = read_all([{"key": "a/b#1"}, {"key": "a/b#2"}], github.get, github.graphql, NOW, 30)
    assert len(result["unknown"]) == 2 and len(github.calls) == 1
