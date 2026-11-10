"""Read the comments and history of GitHub issues and sort them into available, taken, closed and
unknown, so nobody starts work on an issue someone else is already doing."""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Callable

API = "https://api.github.com"
PAUSE_SECONDS = 0.2
RETRY_SECONDS = 3
MAX_TIMELINE_PAGES = 3
MAX_TEXT_CHARS = 8000
MAINTAINERS = {"OWNER", "MEMBER", "COLLABORATOR"}
ISSUE_URL = re.compile(r"github\.com/([^/]+/[^/]+)/issues/(\d+)")

# Someone saying they are doing the issue, or asking for it.
_DO = r"(work on|take|tackle|pick up|handle|fix|give (it|this) a (try|shot|go))"
CLAIM = re.compile(
    rf"(\b(i'?d|i would|i'?d love to|i'?d be happy to|i want to) (like to )?{_DO}\b"
    rf"|\b(can|could|may) i (please )?({_DO}|be assigned)\b"
    rf"|\b(i'?ll|i will|let me) {_DO}\b"
    r"|\bi'?m (working on|taking|on) (this|it)\b|\bi am (working on|taking|on) (this|it)\b"
    r"|\b(i'?ve|i have) (started|begun) (working on|on|looking into) (this|it|a fix)\b"
    r"|\b(i'?m|i am) (currently )?working on (a|the) (fix|pr|pull request|patch)\b"
    r"|\b(i'?ll|i will) (open|submit|raise|send|create|make) (a|the) (pr|pull request|patch)\b"
    r"|\b(i'?m|i am) interested in (working on|taking|fixing|picking up) (this|it)\b"
    r"|\b(please )?assign (this |it |this issue )?to me\b|\bassign me\b"
    r"|^\s*(i'?m )?on it\b|^\s*/(assign|take|claim)\b)",
    re.IGNORECASE | re.MULTILINE)
# Someone asking about the issue without claiming it. With the claims, it shows how many people want it.
INTEREST = re.compile(
    r"\b(is (this|it) (issue )?still (open|available|up for grabs|needed|being worked on)"
    r"|is (anyone|someone|somebody) (still )?working on"
    r"|has (anyone|someone|somebody) (started|picked|taken)"
    r"|any (updates?|progress|news)\b"
    r"|(i'?m|i am) interested\b)",
    re.IGNORECASE)
CROWDED = 3                 # this many outside people asking about one issue: expect competition
SPLIT_INTO_PARTS = 3        # this many PRs mentioning an issue: it is being done in parts
# The same person giving it back.
WITHDRAW = re.compile(
    r"\b(no longer (working|able)|not (working on|able to work on) (this|it) anymore|unassign me"
    r"|feel free to (take|pick)|someone else can (take|pick)|won'?t have time|don'?t have time)\b",
    re.IGNORECASE)

Get = Callable[[str], object]
GraphQL = Callable[[str, dict], dict]

# GitHub's own record of which pull requests will close an issue: "Fixes #12" in the PR, or a link
# made in the sidebar. A PR that only mentions the issue is not in it.
CLOSING_PRS = """query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      closedByPullRequestsReferences(first: 20, includeClosedPrs: true) { nodes { number } }
    }
  }
}"""


class NotFound(Exception):
    pass


class RateLimited(Exception):
    pass


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def is_bot(user: dict | None) -> bool:
    return not user or user.get("type") == "Bot" or user.get("login", "").endswith("[bot]")


# ---- reading the history ------------------------------------------------------------------------

def linked_prs(timeline: list[dict], repo: str, closing: set[int]) -> list[dict]:
    """Pull requests in the same repository that mention the issue, latest state of each, and
    whether each one will close it."""
    prs = {}
    for event in timeline:
        source = (event.get("source") or {}).get("issue") or {}
        if event.get("event") != "cross-referenced" or "pull_request" not in source:
            continue
        if (source.get("repository") or {}).get("full_name", repo).lower() != repo.lower():
            continue
        merged = (source.get("pull_request") or {}).get("merged_at")
        prs[source["number"]] = {
            "number": source["number"],
            "url": source.get("html_url"),
            "author": (source.get("user") or {}).get("login"),
            "state": "merged" if merged else source.get("state"),
            "closes": source["number"] in closing,
            "at": event.get("created_at"),
        }
    return list(prs.values())


def fork_commits(timeline: list[dict], repo: str) -> list[dict]:
    """Commits pushed to someone's fork that mention the issue: work has started, maybe with no PR yet.
    Commits in the repository itself are left out: they are the team's, or a merged PR's."""
    found = {}
    for event in timeline:
        if event.get("event") != "referenced" or is_bot(event.get("actor")):
            continue
        match = re.search(r"/repos/([^/]+/[^/]+)/commits/", event.get("commit_url") or "")
        if not match or match.group(1).lower() == repo.lower():
            continue
        login = (event.get("actor") or {}).get("login")
        found[login] = {"author": login, "fork": match.group(1), "at": event["created_at"]}
    return list(found.values())


def interested(timeline: list[dict], issue_author: str | None) -> list[str]:
    """Outside people who asked for the issue or asked about it, lapsed claims included.
    The issue's author and the team are left out."""
    people = []
    for event in timeline:
        if event.get("event") != "commented" or is_bot(event.get("user")):
            continue
        login = event["user"]["login"]
        if event.get("author_association") in MAINTAINERS or login == issue_author or login in people:
            continue
        body = event.get("body") or ""
        if CLAIM.search(body) or INTEREST.search(body):
            people.append(login)
    return people


def claims(timeline: list[dict]) -> list[dict]:
    """Comments by outside people saying they are doing the issue, unless they later gave it back."""
    found = {}
    for event in timeline:
        if event.get("event") != "commented" or is_bot(event.get("user")):
            continue
        if event.get("author_association") in MAINTAINERS:
            continue
        login, body = event["user"]["login"], event.get("body") or ""
        if WITHDRAW.search(body):
            found.pop(login, None)
        elif CLAIM.search(body):
            found[login] = {"author": login, "at": event["created_at"], "comment": body.strip()[:300]}
    return list(found.values())


def classify(issue: dict, prs: list[dict], claimed: list[dict], now: datetime, claim_days: int,
             forks: list[dict] = (), people: list[str] = ()) -> tuple[str, str, list[str]]:
    """Return (status, reason, notes). Notes are facts that matter but do not decide the status."""
    notes = []

    def recent(at: str | None) -> bool:
        return bool(at) and now - parse_time(at) <= timedelta(days=claim_days)

    if issue.get("state") == "closed":
        return "closed", "issue is closed", notes
    assignees = [a["login"] for a in issue.get("assignees") or []]
    if assignees:
        return "taken", f"assigned to {', '.join(assignees)}", notes
    open_prs = [pr for pr in prs if pr["state"] == "open" and pr["closes"]]
    if open_prs:
        return "taken", "open PR " + ", ".join(f"#{pr['number']} by {pr['author']}" for pr in open_prs), notes
    # A fresh PR that only mentions the issue is usually its fix, written without "Fixes #12".
    # Not when many PRs mention it: then the issue is being done in parts.
    mentioning = [pr for pr in prs if pr["state"] == "open" and recent(pr["at"])]
    if mentioning and len(prs) < SPLIT_INTO_PARTS:
        pr = mentioning[-1]
        days = (now - parse_time(pr["at"])).days
        return "taken", f"open PR #{pr['number']} by {pr['author']} mentions it ({days} days ago)", notes
    fresh_forks = [f for f in forks if recent(f["at"])]
    if fresh_forks:
        f = fresh_forks[-1]
        days = (now - parse_time(f["at"])).days
        return "taken", f"{f['author']} pushed a commit for it to {f['fork']} {days} days ago", notes
    fresh = [c for c in claimed if recent(c["at"])]
    if fresh:
        c = fresh[-1]
        days = (now - parse_time(c["at"])).days
        return "taken", f"{c['author']} asked for it {days} days ago", notes
    for c in claimed:
        notes.append(f"{c['author']} asked for it {(now - parse_time(c['at'])).days} days ago, with no PR since")
    merged_closing = [pr for pr in prs if pr["state"] == "merged" and pr["closes"]]
    if merged_closing:
        pr = merged_closing[-1]
        return "unknown", f"PR #{pr['number']} was merged but the issue is still open; it may be fixed", notes
    notes += pr_notes(prs)
    if len(people) >= CROWDED:
        notes.append(f"crowded: {len(people)} people asked for it or about it ({', '.join(people[:5])}), "
                     "so expect others to try too")
    return "available", "no assignee, open PR or recent claim", notes


def pr_notes(prs: list[dict]) -> list[str]:
    """One note per kind of PR that mentions the issue without closing it. An issue split into many
    small tasks can be mentioned by dozens."""
    def numbers(kind):
        found = [f"#{pr['number']}" for pr in prs if pr["state"] == kind]
        shown = ", ".join(found[:3]) + (f" and {len(found) - 3} more" if len(found) > 3 else "")
        return len(found), shown

    notes = []
    count, shown = numbers("open")
    if count:
        notes.append(f"open PRs that mention it but will not close it: {shown}")
    count, shown = numbers("merged")
    if count:
        notes.append(f"{count} merged PRs mention it, so it may be split into parts: {shown}")
    count, shown = numbers("closed")
    if count:
        notes.append(f"earlier PRs closed without merging: {shown}")
    return notes


def thread_text(issue: dict, timeline: list[dict]) -> str:
    """The issue and its human comments, oldest first, for a person or a model to read."""
    parts = [f"{issue.get('title', '')}\n\n{issue.get('body') or ''}".strip()]
    for event in timeline:
        if event.get("event") == "commented" and not is_bot(event.get("user")):
            who = event["user"]["login"]
            role = " (maintainer)" if event.get("author_association") in MAINTAINERS else ""
            parts.append(f"--- {who}{role}, {event['created_at'][:10]}:\n{(event.get('body') or '').strip()}")
    return "\n\n".join(parts)[:MAX_TEXT_CHARS]


def last_maintainer_comment(timeline: list[dict]) -> str | None:
    times = [e["created_at"] for e in timeline if e.get("event") == "commented"
             and e.get("author_association") in MAINTAINERS and not is_bot(e.get("user"))]
    return max(times, default=None)


# ---- per issue ----------------------------------------------------------------------------------

def locate(entry: dict) -> tuple[str, int] | None:
    """(owner/name, number) from the entry's url or key, or from its item (profile-matcher output)."""
    for source in (entry, entry.get("item") or {}):
        match = ISSUE_URL.search(source.get("url") or "")
        if match:
            return match.group(1), int(match.group(2))
        key = source.get("key") or ""
        if re.fullmatch(r"[^/#\s]+/[^/#\s]+#\d+", key):
            repo, number = key.split("#")
            return repo, int(number)
    return None


def closing_prs(graphql: GraphQL, repo: str, number: int) -> set[int]:
    owner, name = repo.split("/")
    data = graphql(CLOSING_PRS, {"owner": owner, "name": name, "number": number})
    issue = ((data.get("repository") or {}).get("issue")) or {}
    return {node["number"] for node in (issue.get("closedByPullRequestsReferences") or {}).get("nodes", [])}


def read_issue(get: Get, graphql: GraphQL, repo: str, number: int, now: datetime, claim_days: int) -> dict:
    issue = get(f"/repos/{repo}/issues/{number}")
    timeline = []
    for page in range(1, MAX_TIMELINE_PAGES + 1):
        batch = get(f"/repos/{repo}/issues/{number}/timeline?per_page=100&page={page}")
        timeline += batch
        if len(batch) < 100:
            break
    prs, claimed = linked_prs(timeline, repo, closing_prs(graphql, repo, number)), claims(timeline)
    forks = fork_commits(timeline, repo)
    people = interested(timeline, (issue.get("user") or {}).get("login"))
    status, reason, notes = classify(issue, prs, claimed, now, claim_days, forks, people)
    return {
        "status": status, "reason": reason, "notes": notes,
        "linked_prs": prs, "claims": claimed, "fork_commits": forks, "interested": people,
        "comments": issue.get("comments", 0),
        "last_maintainer_comment_at": last_maintainer_comment(timeline),
        "text": thread_text(issue, timeline),
    }


def unread(status_reason: str) -> dict:
    return {"status": "unknown", "reason": status_reason, "notes": [], "linked_prs": [], "claims": [],
            "fork_commits": [], "interested": [], "comments": 0, "last_maintainer_comment_at": None, "text": ""}


def read_all(entries: list[dict], get: Get, graphql: GraphQL, now: datetime, claim_days: int) -> dict:
    result = {"available": [], "taken": [], "closed": [], "unknown": []}
    stop_reason = None
    for n, entry in enumerate(entries, 1):
        where = locate(entry)
        if stop_reason:
            thread = unread(stop_reason)
        elif not where:
            thread = unread("no GitHub issue url or key")
        else:
            try:
                thread = read_issue(get, graphql, *where, now, claim_days)
            except NotFound:
                thread = unread("issue not found")
            except RateLimited as e:
                stop_reason = str(e)
                thread = unread(stop_reason)
            except OSError as e:     # network errors, including HTTP errors
                thread = unread(f"GitHub request failed ({e})")
        result[thread["status"]].append({**entry, "thread": thread})
        name = f"{where[0]}#{where[1]}" if where else "?"
        log(f"{n}/{len(entries)} {thread['status']}: {name} - {thread['reason']}")
    return result


# ---- input and GitHub API -----------------------------------------------------------------------

def entries_from(data) -> list[dict]:
    """Accept a JSON array, or the object written by github-issue-lister (issues), rule-filter
    (kept) or profile-matcher (scored)."""
    if isinstance(data, dict):
        for key in ("scored", "kept", "issues"):
            if key in data:
                return data[key]
        raise ValueError('expected a JSON array or an object with "issues", "kept" or "scored"')
    return data


def github_token() -> str | None:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        return token
    try:
        result = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10)
        return result.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def make_client(token: str) -> tuple[Get, GraphQL]:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "issue-reader",
               "X-GitHub-Api-Version": "2022-11-28", "Authorization": f"Bearer {token}"}

    def get(path: str, body: bytes | None = None):
        request = urllib.request.Request(API + path, data=body, headers=headers)
        for attempt in range(2):
            time.sleep(PAUSE_SECONDS)
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                if e.code in (404, 410):
                    raise NotFound(path) from e
                if e.code in (403, 429) and e.headers.get("x-ratelimit-remaining") == "0":
                    raise RateLimited("GitHub rate limit reached") from e
                if e.code < 500 or attempt:
                    raise
            except OSError:      # dropped connection or timeout: try once more
                if attempt:
                    raise
            time.sleep(RETRY_SECONDS)

    def graphql(query: str, variables: dict) -> dict:
        answer = get("/graphql", json.dumps({"query": query, "variables": variables}).encode("utf-8"))
        if answer.get("errors"):
            if any(e.get("type") == "NOT_FOUND" for e in answer["errors"]):
                raise NotFound(variables)
            if any(e.get("type") == "RATE_LIMITED" for e in answer["errors"]):
                raise RateLimited("GitHub rate limit reached")
            raise OSError(answer["errors"][0].get("message", "GraphQL error"))
        return answer["data"]

    return get, graphql


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("urls", nargs="*", help="issue links; without these, JSON is read from stdin")
    parser.add_argument("--limit", type=int, default=20, help="read only the first N issues")
    parser.add_argument("--claim-days", type=int, default=30,
                        help="a request to work on the issue older than this, with no PR, has lapsed")
    args = parser.parse_args()

    if args.urls:
        entries = [{"url": url} for url in args.urls]
    else:
        entries = entries_from(json.loads(sys.stdin.buffer.read().decode("utf-8-sig")))
    token = github_token()
    if not token:
        sys.exit("No GitHub token found. Set GITHUB_TOKEN or run `gh auth login`: GitHub's GraphQL API needs one.")
    get, graphql = make_client(token)
    result = read_all(entries[:args.limit], get, graphql, datetime.now(timezone.utc), args.claim_days)
    sys.stdout.reconfigure(encoding="utf-8")
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
