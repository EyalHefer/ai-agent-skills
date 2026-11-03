"""Find popular, recently active GitHub repositories that have open issues labeled for newcomers."""

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Callable

API = "https://api.github.com"
PER_PAGE = 100
MAX_RESULTS = 1000          # GitHub search never returns more than this per query
PAUSE_SECONDS = 2.5         # the search API allows 30 requests a minute
SORTS = ["updated", "stars", "good-first-issues", "help-wanted-issues"]

Search = Callable[[dict], dict]   # query parameters -> parsed JSON


class RateLimited(Exception):
    pass


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def build_query(language: str, args: argparse.Namespace, today: datetime, label: str) -> str:
    """label is "good-first-issues" or "help-wanted-issues", GitHub's counts of open issues with
    those labels."""
    stars = f"{args.min_stars}..{args.max_stars}" if args.max_stars else f">={args.min_stars}"
    pushed = (today - timedelta(days=args.pushed_days)).date().isoformat()
    created = (today - timedelta(days=args.min_age_days)).date().isoformat()
    parts = [f'language:"{language}"', f"stars:{stars}", f"pushed:>={pushed}", f"created:<={created}",
             f"{label}:>={args.min_issues}", "archived:false", "is:public", "fork:false"]
    parts += [f'topic:"{topic}"' for topic in args.topic]
    return " ".join(parts)


def repo_record(item: dict, language: str) -> dict:
    """The fields later skills and the agent need, under GitHub's own names."""
    return {
        "full_name": item["full_name"],
        "url": item["html_url"],
        "description": item.get("description") or "",
        "language": item.get("language") or language,
        "searched_language": language,
        "stars": item["stargazers_count"],
        "forks": item["forks_count"],
        "open_issues": item["open_issues_count"],
        "topics": item.get("topics", []),
        "license": (item.get("license") or {}).get("spdx_id"),
        "pushed_at": item["pushed_at"],
        "created_at": item["created_at"],
    }


def search_language(search: Search, query: str, sort: str, limit: int) -> tuple[list[dict], int]:
    """(items, total_count), reading pages until limit items or the results end."""
    items, total, page = [], 0, 1
    while len(items) < limit and page * PER_PAGE <= MAX_RESULTS:
        data = search({"q": query, "sort": sort, "order": "desc", "per_page": PER_PAGE, "page": page})
        total = data.get("total_count", 0)
        items += data.get("items", [])
        if len(data.get("items", [])) < PER_PAGE:
            break
        page += 1
    return items[:limit], total


def find(search: Search, args: argparse.Namespace, today: datetime) -> dict:
    labels = ["good-first-issues"] + (["help-wanted-issues"] if args.help_wanted else [])
    repos: dict[str, dict] = {}
    totals = {}
    for language in args.language:
        found = 0
        for label in labels:
            query = build_query(language, args, today, label)
            try:
                items, total = search_language(search, query, args.sort, args.per_language)
            except RateLimited as e:
                log(str(e))
                return {"repos": list(repos.values()), "totals": totals, "stopped": str(e)}
            totals[f"{language} {label}"] = total
            for item in items:
                if item["full_name"] not in repos:
                    repos[item["full_name"]] = repo_record(item, language)
                    found += 1
        log(f"{language}: {found} repositories")
    return {"repos": list(repos.values()), "totals": totals, "stopped": None}


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


def make_search(token: str | None) -> Search:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "github-repo-finder",
               "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    def search(params: dict) -> dict:
        url = f"{API}/search/repositories?{urllib.parse.urlencode(params)}"
        for attempt in range(2):
            time.sleep(PAUSE_SECONDS)
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
                    return json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                if e.code >= 500 and not attempt:
                    continue
                if e.code not in (403, 429):
                    raise
                wait = int(e.headers.get("retry-after") or 0) or \
                    max(0, int(e.headers.get("x-ratelimit-reset", "0")) - int(time.time())) + 1
                if attempt or wait > 90:
                    raise RateLimited(f"GitHub search limit reached, try again in {wait} seconds") from e
                log(f"Search limit reached, waiting {wait} seconds")
                time.sleep(wait)
            except OSError:      # dropped connection or timeout: try once more
                if attempt:
                    raise
        raise AssertionError("unreachable")
    return search


# ---- command line -------------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--language", action="append", required=True,
                        help='GitHub language name, repeatable: Python, Java, "C++", JavaScript, C')
    parser.add_argument("--min-stars", type=int, default=1000)
    parser.add_argument("--max-stars", type=int, help="skip the very largest projects")
    parser.add_argument("--pushed-days", type=int, default=30, help="pushed to within this many days")
    parser.add_argument("--min-age-days", type=int, default=365,
                        help="skip younger repositories, whose stars often come from hype")
    parser.add_argument("--min-issues", type=int, default=1, help="open newcomer issues, at least")
    parser.add_argument("--help-wanted", action="store_true", help='also repos with "help wanted" issues')
    parser.add_argument("--topic", action="append", default=[], help="GitHub topic, repeatable, all must match")
    parser.add_argument("--sort", choices=SORTS, default="updated")
    parser.add_argument("--per-language", type=int, default=20, help="repositories per language and label")
    args = parser.parse_args()

    token = github_token()
    if not token:
        log("No GitHub token found (GITHUB_TOKEN or `gh auth login`). Search allows 10 requests a minute without one.")
    result = find(make_search(token), args, datetime.now(timezone.utc))
    sys.stdout.reconfigure(encoding="utf-8")
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
