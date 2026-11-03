"""List the open, unassigned issues that GitHub repositories label for newcomers."""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Callable

API = "https://api.github.com"
PAUSE_SECONDS = 0.2
RETRY_SECONDS = 3
MAX_LABEL_PAGES = 5
MAX_BODY_CHARS = 4000

# Matched against the start of a label name, lowercased, with "-", "_", ":", "/" and runs of spaces read as one space.
# Anchored, so "I-lang-easy-decision" and "D-newcomer-roadblock" are not taken for beginner labels.
BEGINNER_LABEL = re.compile(
    r"^(good first (issue|bug|contribution|pr)|first timers? only|first issue|beginner|easy\b|e easy|"
    r"difficulty easy|level easy|complexity low|starter|newcomer friendly|good for newcomers|"
    r"low hanging fruit)")
HELP_WANTED_LABEL = re.compile(r"^(help wanted|help needed|e help wanted|status help wanted)\b")
HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)

Get = Callable[[str], object]


class NotFound(Exception):
    pass


class RateLimited(Exception):
    pass


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


# ---- choosing labels ----------------------------------------------------------------------------

def normalize(label: str) -> str:
    return re.sub(r"[-_:/\s]+", " ", label.lower()).strip()


def newcomer_labels(names: list[str], help_wanted: bool, extra: list[str]) -> list[str]:
    """The repository's own label names that mark issues for newcomers."""
    extra_lower = {e.lower() for e in extra}
    chosen = []
    for name in names:
        n = normalize(name)
        if BEGINNER_LABEL.match(n) or (help_wanted and HELP_WANTED_LABEL.match(n)) or name.lower() in extra_lower:
            chosen.append(name)
    return chosen


# ---- issues -------------------------------------------------------------------------------------

def clean_body(body: str | None) -> str:
    """Issue templates leave HTML comments with instructions; they are noise for a reader or a model."""
    text = HTML_COMMENT.sub("", body or "")
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:MAX_BODY_CHARS]


def issue_record(item: dict, repo: dict, matched: list[str]) -> dict:
    labels = [label["name"] for label in item.get("labels", [])]
    return {
        "key": f"{repo['full_name']}#{item['number']}",
        "url": item["html_url"],
        "title": item["title"],
        "repo": repo["full_name"],
        "repo_language": repo.get("language"),
        "repo_stars": repo.get("stars"),
        "number": item["number"],
        "labels": labels,
        "newcomer_labels": [name for name in labels if name in matched],
        "body": clean_body(item.get("body")),
        "comments": item.get("comments", 0),
        "author_association": item.get("author_association"),
        "created_at": item["created_at"],
        "updated_at": item["updated_at"],
    }


def is_available(item: dict) -> bool:
    """Pull requests appear in the issues API too. Assigned or locked issues are taken."""
    return "pull_request" not in item and not item.get("assignees") and not item.get("assignee") \
        and not item.get("locked")


def list_repo(get: Get, repo: dict, args: argparse.Namespace, now: datetime) -> tuple[list[dict], list[str]]:
    name = repo["full_name"]
    label_names = []
    for page in range(1, MAX_LABEL_PAGES + 1):
        batch = get(f"/repos/{name}/labels?per_page=100&page={page}")
        label_names += [label["name"] for label in batch]
        if len(batch) < 100:
            break
    matched = newcomer_labels(label_names, args.help_wanted, args.label)

    # An issue labeled for newcomers that nobody solved in years is rarely easy.
    oldest = (now - timedelta(days=args.max_age_days)).isoformat().replace("+00:00", "Z")
    found: dict[int, dict] = {}
    for label in matched:
        query = urllib.parse.urlencode({"labels": label, "state": "open", "assignee": "none",
                                        "sort": "created", "direction": "desc", "per_page": 100})
        for item in get(f"/repos/{name}/issues?{query}"):
            if is_available(item) and item["created_at"] >= oldest and item["number"] not in found:
                found[item["number"]] = issue_record(item, repo, matched)
    issues = sorted(found.values(), key=lambda i: i["created_at"], reverse=True)
    return issues[:args.per_repo], matched


# ---- input --------------------------------------------------------------------------------------

def repos_from(data, include: list[str]) -> list[dict]:
    """Accept a repo-health-checker result (the statuses in include), a github-repo-finder result
    (repos), a rule-filter result (kept), or a JSON array of names or objects with full_name."""
    if isinstance(data, dict):
        if "healthy" in data:
            entries = [e for status in include for e in data.get(status, [])]
        elif "repos" in data:
            entries = data["repos"]
        elif "kept" in data:
            entries = data["kept"]
        else:
            raise ValueError('expected a JSON array or an object with "healthy", "repos" or "kept"')
    else:
        entries = data
    repos = []
    for entry in entries:
        if isinstance(entry, str):
            entry = {"full_name": entry}
        repos.append({"full_name": entry["full_name"], "language": entry.get("language"),
                      "stars": entry.get("stars")})
    return repos


def list_all(repos: list[dict], get: Get, args: argparse.Namespace, now: datetime) -> dict:
    result = {"issues": [], "repos": []}
    stop_reason = None
    for n, repo in enumerate(repos, 1):
        summary = {"full_name": repo["full_name"], "labels": [], "issues": 0, "error": None}
        if stop_reason:
            summary["error"] = stop_reason
        else:
            try:
                issues, summary["labels"] = list_repo(get, repo, args, now)
                summary["issues"] = len(issues)
                result["issues"] += issues
            except NotFound:
                summary["error"] = "repository not found"
            except RateLimited as e:
                stop_reason = summary["error"] = str(e)
            except OSError as e:     # network errors, including HTTP errors
                summary["error"] = f"GitHub request failed ({e})"
        result["repos"].append(summary)
        log(f"{n}/{len(repos)} {repo['full_name']}: {summary['error'] or str(summary['issues']) + ' issues'}"
            f"{' via ' + ', '.join(summary['labels']) if summary['labels'] else ''}")
    return result


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
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "github-issue-lister",
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
                    raise RateLimited("GitHub rate limit reached") from e
                if e.code < 500 or attempt:
                    raise
            except OSError:      # dropped connection or timeout: try once more
                if attempt:
                    raise
            time.sleep(RETRY_SECONDS)
    return get


# ---- command line -------------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repos", nargs="*", help="owner/name; without these, JSON is read from stdin")
    parser.add_argument("--include", action="append", choices=["healthy", "unhealthy", "inactive", "unknown"],
                        help="repo-health-checker statuses to use (default healthy), repeatable")
    parser.add_argument("--help-wanted", action="store_true", help='also issues labeled "help wanted"')
    parser.add_argument("--label", action="append", default=[], help="another label name to use, repeatable")
    parser.add_argument("--per-repo", type=int, default=10, help="newest issues kept per repository")
    parser.add_argument("--max-age-days", type=int, default=365, help="skip issues opened longer ago")
    args = parser.parse_args()

    data = args.repos or json.loads(sys.stdin.buffer.read().decode("utf-8-sig"))
    repos = repos_from(data, args.include or ["healthy"])
    token = github_token()
    if not token:
        log("No GitHub token found (GITHUB_TOKEN or `gh auth login`). Without one only 60 requests an hour are allowed.")
    result = list_all(repos, make_get(token), args, datetime.now(timezone.utc))
    sys.stdout.reconfigure(encoding="utf-8")
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
