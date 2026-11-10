"""Measure how quickly the maintainers of GitHub repositories answer outside contributors, and sort
the repositories into healthy, slow, inactive and unknown."""

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Callable

API = "https://api.github.com"
MAINTAINERS = {"OWNER", "MEMBER", "COLLABORATOR"}
# Only people with triage rights can do these to someone else's issue, so they show a maintainer saw it.
TRIAGE_EVENTS = {"labeled", "unlabeled", "assigned", "milestoned", "closed", "merged"}
MAX_LIST_PAGES = 5
PAUSE_SECONDS = 0.2         # between API calls, to be polite
RETRY_SECONDS = 3
GUIDE_FOLDERS = ["", ".github", "docs"]
GUIDE_FILE = re.compile(r"^(contributing|ai[-_ ]?policy)", re.IGNORECASE)
MIN_DECIDED_PRS = 5         # fewer closed outside PRs than this says nothing about the merge rate
MIN_NEWCOMER_PRS = 4        # the same for PRs by first-time contributors, who are fewer
# GitHub gives the author's association as it is now, not when the PR was opened. These authors have
# no merged work in the repository even now, so they were newcomers then too. A CONTRIBUTOR may have
# become one with this very PR, which is checked from the commit history.
NEWCOMER_ASSOCIATIONS = {"FIRST_TIME_CONTRIBUTOR", "FIRST_TIMER", "NONE"}
AI_WORD = re.compile(r"\b(AI|LLMs?|ChatGPT|Copilot|Claude|generative|AI-generated|machine-generated)\b")
# A line about AI is a policy only when it talks about how contributions are made.
# "Contributing to Acme AI" is a product name.
POLICY_WORD = re.compile(r"(generat|assist|tool|polic|disclos|writ|use of|using|allowed|prohibit|\bban)",
                         re.IGNORECASE)
FREQUENT_AUTHOR_ITEMS = 5   # someone who opened this many items in the window is treated as staff
CLA = re.compile(r"\b(CLA|Contributor License Agreement)\b", re.IGNORECASE)
MAX_POLICY_LINES = 5

Get = Callable[[str], object]   # API path -> parsed JSON, raises NotFound for 404


class NotFound(Exception):
    pass


class RateLimited(Exception):
    pass


class TooBusy(Exception):
    pass


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def days_between(start: datetime, end: datetime) -> float:
    return round((end - start).total_seconds() / 86400, 1)


# ---- what counts as an answer -------------------------------------------------------------------

def is_bot(user: dict | None) -> bool:
    return not user or user.get("type") == "Bot" or user.get("login", "").endswith("[bot]")


def is_outside_contribution(item: dict) -> bool:
    """Issues and PRs opened by maintainers or bots do not wait for an answer, so they are not counted."""
    return item.get("author_association") not in MAINTAINERS and not is_bot(item.get("user"))


def first_response(item: dict, timeline: list[dict]) -> datetime | None:
    """When a maintainer first reacted: a comment or review, or labeling, assigning, closing or
    merging. Bots and the author do not count."""
    author = (item.get("user") or {}).get("login")
    times = []
    for event in timeline:
        kind = event.get("event")
        if kind in ("commented", "reviewed"):
            who, when = event.get("user") or event.get("actor"), event.get("created_at") or event.get("submitted_at")
            if event.get("author_association") not in MAINTAINERS:
                continue
        elif kind in TRIAGE_EVENTS:
            who, when = event.get("actor"), event.get("created_at")
        else:
            continue
        if when and not is_bot(who) and who.get("login") != author:
            times.append(parse_time(when))
    return min(times, default=None)


# ---- turning a sample into numbers --------------------------------------------------------------

def response_days(item: dict, timeline: list[dict], now: datetime) -> tuple[float, bool]:
    """(days until the first answer, answered). An unanswered item counts its age so far, which is
    the least it will end up waiting."""
    created = parse_time(item["created_at"])
    answered_at = first_response(item, timeline)
    if answered_at:
        return days_between(created, answered_at), True
    return days_between(created, now), False


def merge_stats(prs: list[dict]) -> dict:
    merged = [pr for pr in prs if (pr.get("pull_request") or {}).get("merged_at")]
    rejected = [pr for pr in prs if pr.get("state") == "closed" and pr not in merged]
    merge_days = [days_between(parse_time(pr["created_at"]), parse_time(pr["pull_request"]["merged_at"]))
                  for pr in merged]
    decided = len(merged) + len(rejected)
    return {
        "merged_prs": len(merged),
        "closed_unmerged_prs": len(rejected),
        "open_prs": len(prs) - decided,
        "median_merge_days": median(merge_days),
        "merge_rate": round(len(merged) / decided, 2) if decided else None,
    }


def was_newcomer(get: Get, name: str, pr: dict) -> bool:
    """Whether the PR's author had nothing merged in the repository when they opened it.
    A merged PR by a CONTRIBUTOR costs one request: their commits from before the PR. Other PRs by a
    CONTRIBUTOR are counted as not a newcomer's, since their merged work most likely came first."""
    association = pr.get("author_association")
    if association in NEWCOMER_ASSOCIATIONS:
        return True
    if association != "CONTRIBUTOR" or not (pr.get("pull_request") or {}).get("merged_at"):
        return False
    login = (pr.get("user") or {}).get("login")
    query = urllib.parse.urlencode({"author": login, "until": pr["created_at"], "per_page": 1})
    return not get(f"/repos/{name}/commits?{query}")


def newcomer_stats(prs: list[dict], waits: list[tuple[float, bool]]) -> dict:
    """The PR numbers again, for first-time contributors only. They are the ones a newcomer will be
    treated like; regular outside contributors are often answered faster."""
    stats = merge_stats(prs)
    return {
        "newcomer_prs": len(prs),
        "newcomer_merged_prs": stats["merged_prs"],
        "newcomer_closed_unmerged_prs": stats["closed_unmerged_prs"],
        "newcomer_merge_rate": stats["merge_rate"],
        "newcomer_median_first_response_days": median([days for days, _ in waits]),
    }


def median(values: list[float]) -> float | None:
    return round(statistics.median(values), 1) if values else None


def classify(repo: dict, metrics: dict, now: datetime, limits: argparse.Namespace) -> tuple[str, str]:
    """Return (status, reason), where status is "healthy", "unhealthy", "inactive" or "unknown"."""
    if repo.get("archived"):
        return "inactive", "repository is archived"
    idle = days_between(parse_time(repo["pushed_at"]), now)
    if idle > limits.inactive_days:
        return "inactive", f"no push for {idle:.0f} days"
    sample = metrics["sample_issues"] + metrics["sample_prs"]
    if sample < limits.min_sample:
        return "unknown", f"only {sample} outside issues and PRs in the last {limits.days} days"
    if metrics["answered"] == 0:
        return "inactive", f"no maintainer answered any of {sample} outside issues and PRs"

    response, merge = metrics["median_first_response_days"], metrics["median_merge_days"]
    problems = []
    decided = metrics["merged_prs"] + metrics["closed_unmerged_prs"]
    if decided >= MIN_DECIDED_PRS and metrics["merge_rate"] < limits.min_merge_rate:
        problems.append(f"merged only {metrics['merged_prs']} of {decided} closed outside PRs")
    elif newcomers_rejected(metrics, limits.min_newcomer_merge_rate):     # already said, if all were rejected
        problems.append(f"merged only {metrics['newcomer_merged_prs']} of {newcomer_decided(metrics)} closed PRs "
                        "by first-time contributors")
    if response > limits.max_response_days:
        problems.append(f"first answer takes {response} days (median)")
    elif newcomers_wait(metrics, limits.max_response_days):
        problems.append(f"first answer to first-time contributors' PRs takes "
                        f"{metrics['newcomer_median_first_response_days']} days (median)")
    if merge is not None and merge > limits.max_merge_days:
        problems.append(f"merging takes {merge} days (median)")
    if problems:
        return "unhealthy", "; ".join(problems)
    merge_text = f", merge in {merge} days" if merge is not None else ", no merged outside PRs to measure"
    return "healthy", f"first answer in {response} days{merge_text} (medians){newcomer_text(metrics)}"


def newcomer_decided(metrics: dict) -> int:
    return metrics.get("newcomer_merged_prs", 0) + metrics.get("newcomer_closed_unmerged_prs", 0)


def newcomers_rejected(metrics: dict, min_rate: float) -> bool:
    return newcomer_decided(metrics) >= MIN_NEWCOMER_PRS and metrics["newcomer_merge_rate"] < min_rate


def newcomers_wait(metrics: dict, max_days: float) -> bool:
    days = metrics.get("newcomer_median_first_response_days")
    return metrics.get("newcomer_prs", 0) >= MIN_NEWCOMER_PRS and days is not None and days > max_days


def newcomer_text(metrics: dict) -> str:
    parts = []
    if metrics.get("newcomer_prs", 0) >= MIN_NEWCOMER_PRS:
        parts.append(f"first answer in {metrics['newcomer_median_first_response_days']} days")
    if newcomer_decided(metrics):
        parts.append(f"{metrics['newcomer_merged_prs']} of {newcomer_decided(metrics)} closed PRs merged")
    return f"; first-time contributors: {', '.join(parts)}" if parts else ""


# ---- contributing guide -------------------------------------------------------------------------

def guide_paths(listings: dict[str, list[dict]]) -> list[dict]:
    """Contributing guides and AI policies among the files of the given folders."""
    return [entry for folder in GUIDE_FOLDERS for entry in listings.get(folder, [])
            if entry.get("type") == "file" and GUIDE_FILE.match(entry["name"])]


def policy_notes(texts: dict[str, str]) -> dict:
    """Lines that mention AI or a CLA, for a person or a model to read: "we do not have a CLA" also
    mentions one. A file that is itself an AI policy is listed by name, since all of it is about AI."""
    lines, cla = [], []
    for path, text in texts.items():
        cla += [line.strip() for line in text.splitlines() if CLA.search(line)]
        if re.match(r"ai[-_ ]?policy", path.rsplit("/", 1)[-1], re.IGNORECASE):
            lines.append(f"has an AI policy: {path}")
        else:
            lines += [line.strip() for line in text.splitlines() if is_ai_policy_line(line)]
    return {"files": list(texts), "cla": cla[:MAX_POLICY_LINES], "ai_policy": lines[:MAX_POLICY_LINES]}


def is_ai_policy_line(line: str) -> bool:
    """Headings are skipped: "# Contributing to Acme AI" names a product, not a rule."""
    line = line.strip()
    return not line.startswith("#") and bool(AI_WORD.search(line)) and bool(POLICY_WORD.search(line))


def contributing_guide(get: Get, name: str) -> dict:
    listings = {}
    for folder in GUIDE_FOLDERS:
        try:
            listing = get(f"/repos/{name}/contents/{folder}")
        except NotFound:
            continue
        listings[folder] = listing if isinstance(listing, list) else []
    texts = {entry["path"]: get_text(entry["download_url"]) for entry in guide_paths(listings)}
    return policy_notes(texts)


# ---- reading a repository -----------------------------------------------------------------------

def recent_outside_items(get: Get, name: str, now: datetime, limits: argparse.Namespace) -> tuple[list, list]:
    """Outside issues and PRs opened in the window. Items younger than min_age_days are skipped,
    because they have not had a fair chance to be answered yet. Frequent authors are skipped too:
    see frequent_authors."""
    start, end = now - timedelta(days=limits.days), now - timedelta(days=limits.min_age_days)
    fetched = []
    for page in range(1, MAX_LIST_PAGES + 1):
        batch = get(f"/repos/{name}/issues?state=all&sort=created&direction=desc&per_page=100&page={page}")
        fetched += [item for item in batch if parse_time(item["created_at"]) >= start]
        issues, prs = split_outside(fetched, end)
        enough = len(issues) >= limits.sample and len(prs) >= limits.sample
        if enough or len(batch) < 100 or parse_time(batch[-1]["created_at"]) < start:
            break
    else:
        if parse_time(batch[-1]["created_at"]) > end:
            raise TooBusy(f"too busy to sample: over {MAX_LIST_PAGES * 100} issues and PRs "
                          f"in the last {limits.min_age_days} days")
    return issues[:limits.sample], prs[:limits.sample]


def frequent_authors(items: list[dict]) -> set[str]:
    """People who opened many of the items. Company projects often hide their staff's membership,
    so staff appear as outside contributors and their PRs, merged by colleagues within hours, make
    the project look fast. Whoever opens this many items is treated as part of the team."""
    counts = Counter((item.get("user") or {}).get("login") for item in items)
    return {login for login, count in counts.items() if login and count >= FREQUENT_AUTHOR_ITEMS}


def split_outside(items: list[dict], end: datetime) -> tuple[list, list]:
    team = frequent_authors(items)
    issues, prs = [], []
    for item in items:
        author = (item.get("user") or {}).get("login")
        if parse_time(item["created_at"]) <= end and is_outside_contribution(item) and author not in team:
            (prs if "pull_request" in item else issues).append(item)
    return issues, prs


def check_repo(get: Get, name: str, now: datetime, limits: argparse.Namespace) -> dict:
    repo = get(f"/repos/{name}")
    issues, prs = recent_outside_items(get, name, now, limits)
    waits = [response_days(item, get(f"/repos/{name}/issues/{item['number']}/timeline?per_page=100"), now)
             for item in issues + prs]
    newcomer = [n for n, pr in enumerate(prs) if was_newcomer(get, name, pr)]
    pr_waits = waits[len(issues):]
    metrics = {
        "stars": repo["stargazers_count"],
        "pushed_at": repo["pushed_at"],
        "sample_issues": len(issues),
        "sample_prs": len(prs),
        "answered": sum(1 for _, answered in waits if answered),
        "median_first_response_days": median([days for days, _ in waits]),
        **merge_stats(prs),
        **newcomer_stats([prs[n] for n in newcomer], [pr_waits[n] for n in newcomer]),
    }
    status, reason = classify(repo, metrics, now, limits)
    return {"status": status, "reason": reason, "metrics": metrics,
            "contributing": contributing_guide(get, name)}


# ---- GitHub API ---------------------------------------------------------------------------------

def github_token() -> str | None:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        return token
    try:
        result = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10)
        return result.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def make_get(token: str | None) -> Get:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "repo-health-checker",
               "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    def get(path: str):
        request = urllib.request.Request(API + path, headers=headers)
        for attempt in range(2):
            time.sleep(PAUSE_SECONDS)
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    raise NotFound(path) from e
                if e.code in (403, 429) and e.headers.get("x-ratelimit-remaining") == "0":
                    reset = int(e.headers.get("x-ratelimit-reset", "0"))
                    raise RateLimited(f"GitHub rate limit reached, resets at {datetime.fromtimestamp(reset):%H:%M}") from e
                if e.code < 500 or attempt:
                    raise
            except OSError:      # dropped connection or timeout: try once more
                if attempt:
                    raise
            time.sleep(RETRY_SECONDS)
    return get


def get_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "repo-health-checker"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8", errors="replace")


# ---- command line -------------------------------------------------------------------------------

def entries_from(data) -> list:
    """Accept a JSON array of repos, or the object written by github-repo-finder (repos) or
    rule-filter (kept)."""
    if isinstance(data, dict):
        for key in ("repos", "kept"):
            if key in data:
                return data[key]
        raise ValueError('expected a JSON array or an object with "repos" or "kept"')
    return data


def repo_name(entry, field: str) -> str | None:
    if isinstance(entry, str):
        return entry
    return entry.get(field) or (entry.get("item") or {}).get(field)


def check_all(entries: list, get: Get, now: datetime, limits: argparse.Namespace) -> dict:
    result = {"healthy": [], "unhealthy": [], "inactive": [], "unknown": []}
    stop_reason = None
    for n, entry in enumerate(entries, 1):
        name = repo_name(entry, limits.repo_field)
        if stop_reason:
            health = {"status": "unknown", "reason": stop_reason}
        elif not name:
            health = {"status": "unknown", "reason": f'no "{limits.repo_field}" field'}
        else:
            try:
                health = check_repo(get, name, now, limits)
            except NotFound:
                health = {"status": "unknown", "reason": "repository not found"}
            except TooBusy as e:
                health = {"status": "unknown", "reason": str(e)}
            except RateLimited as e:
                stop_reason = str(e)
                health = {"status": "unknown", "reason": stop_reason}
            except OSError as e:     # network errors, including HTTP errors
                health = {"status": "unknown", "reason": f"GitHub request failed ({e})"}
        base = {"full_name": entry} if isinstance(entry, str) else entry
        result[health["status"]].append({**base, "health": health})
        log(f"{n}/{len(entries)} {health['status']}: {name} - {health['reason']}")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repos", nargs="*", help="owner/name; without these, a JSON array is read from stdin")
    parser.add_argument("--repo-field", default="full_name")
    parser.add_argument("--days", type=int, default=90, help="look at issues and PRs from this many days back")
    parser.add_argument("--min-age-days", type=int, default=7, help="skip items younger than this")
    parser.add_argument("--sample", type=int, default=15, help="issues and PRs checked per repo, each")
    parser.add_argument("--min-sample", type=int, default=5, help="fewer items than this is unknown")
    parser.add_argument("--max-response-days", type=float, default=5)
    parser.add_argument("--max-merge-days", type=float, default=21)
    parser.add_argument("--min-merge-rate", type=float, default=0.2,
                        help="share of closed outside PRs that were merged")
    parser.add_argument("--min-newcomer-merge-rate", type=float, default=0.2,
                        help="share of closed PRs by first-time contributors that were merged")
    parser.add_argument("--inactive-days", type=int, default=60, help="no push for longer is inactive")
    args = parser.parse_args()

    entries = args.repos or entries_from(json.loads(sys.stdin.buffer.read().decode("utf-8-sig")))
    token = github_token()
    if not token:
        log("No GitHub token found (GITHUB_TOKEN or `gh auth login`). Without one only 60 requests an hour are allowed.")
    result = check_all(entries, make_get(token), datetime.now(timezone.utc), args)
    sys.stdout.reconfigure(encoding="utf-8")
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
