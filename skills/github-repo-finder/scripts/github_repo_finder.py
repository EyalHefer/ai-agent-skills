"""Find popular, recently active GitHub repositories that have open issues labeled for newcomers.

Two kinds of source: GitHub's repository search, which only knows the exact "good first issue" and
"help wanted" labels, and curated lists of newcomer-friendly projects, which also hold projects that
label their issues "beginner", "easy" and so on."""

import argparse
import http.client
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
PER_PAGE = 100
MAX_RESULTS = 1000          # GitHub search never returns more than this per query
PAUSE_SECONDS = 2.5         # the search API allows 30 requests a minute
SORTS = ["updated", "stars", "good-first-issues", "help-wanted-issues"]
SOURCES = ["search", "goodfirstissue", "upforgrabs"]
CURATED_URLS = {
    "goodfirstissue": "https://raw.githubusercontent.com/deepsourcelabs/good-first-issue/master/data/repositories.toml",
    "upforgrabs": "https://up-for-grabs.net/javascripts/projects.json",
}
# Newcomer labels counted for repositories from curated lists. GitHub matches label names exactly,
# ignoring case. The label an up-for-grabs project names for itself is added to these.
NEWCOMER_LABELS = ["good first issue", "good-first-issue", "good first contribution", "beginner",
                   "beginner-friendly", "easy", "E-easy", "difficulty: easy", "first-timers-only",
                   "starter", "low-hanging-fruit", "newcomer"]
HELP_WANTED_LABELS = ["help wanted", "help-wanted", "status: help wanted"]
GRAPHQL_BATCH = 40          # repositories looked up per GraphQL request
GITHUB_REPO = re.compile(r"github\.com/([\w.-]+)/([\w.-]+)", re.IGNORECASE)

Search = Callable[[dict], dict]     # query parameters -> parsed JSON
GraphQL = Callable[[str], dict]     # query -> the whole answer, with "data" and maybe "errors"
Fetch = Callable[[str], str]        # url -> text


class RateLimited(Exception):
    pass


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


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


def repo_record(item: dict, language: str, source: str = "search") -> dict:
    """The fields later skills and the agent need, under GitHub's own names."""
    return {
        "full_name": item["full_name"],
        "url": item["html_url"],
        "description": item.get("description") or "",
        "language": item.get("language") or language,
        "searched_language": language,
        "source": source,
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


def find(search: Search, args: argparse.Namespace, today: datetime,
         graphql: GraphQL | None = None, fetch: Fetch | None = None) -> dict:
    """Runs each source in turn. A repository found by two sources is returned once, as the first found it."""
    repos: dict[str, dict] = {}
    totals: dict[str, int] = {}
    stopped = None
    for source in args.source:
        if source == "search":
            stopped = find_by_search(search, args, today, repos, totals)
        else:
            stopped = find_in_list(source, graphql, fetch, args, today, repos, totals)
        if stopped:
            break
    return {"repos": list(repos.values()), "totals": totals, "stopped": stopped}


def find_by_search(search: Search, args: argparse.Namespace, today: datetime, repos: dict,
                   totals: dict) -> str | None:
    labels = ["good-first-issues"] + (["help-wanted-issues"] if args.help_wanted else [])
    for language in args.language:
        found = 0
        for label in labels:
            query = build_query(language, args, today, label)
            try:
                items, total = search_language(search, query, args.sort, args.per_language)
            except RateLimited as e:
                log(str(e))
                return str(e)
            totals[f"{language} {label}"] = total
            for item in items:
                if item["full_name"] not in repos:
                    repos[item["full_name"]] = repo_record(item, language)
                    found += 1
        log(f"{language}: {found} repositories")
    return None


# ---- curated lists ------------------------------------------------------------------------------

def parse_goodfirstissue(text: str) -> dict[str, str | None]:
    """repositories.toml of goodfirstissue.dev: one 'github.com/owner/name' per line.
    Returns {owner/name: the project's own newcomer label, or None}."""
    return {f"{m[1]}/{m[2]}": None for m in GITHUB_REPO.finditer(text)}


def parse_upforgrabs(text: str) -> dict[str, str | None]:
    """projects.json of up-for-grabs.net: each project names its site and its newcomer label."""
    found = {}
    for project in json.loads(text).values():
        grabs = project.get("upforgrabs") or {}
        for url in (project.get("site") or "", grabs.get("link") or ""):
            match = GITHUB_REPO.search(url)
            if match:
                found.setdefault(f"{match[1]}/{match[2].removesuffix('.git')}", grabs.get("name"))
                break
    return found


PARSERS = {"goodfirstissue": parse_goodfirstissue, "upforgrabs": parse_upforgrabs}


# What the requirements are checked on. Asked for every listed repository, so kept short.
CHECK_FIELDS = ("nameWithOwner stargazerCount pushedAt createdAt isArchived isFork isPrivate"
                " primaryLanguage { name } repositoryTopics(first: 20) { nodes { topic { name } } }")
# The rest, asked only for repositories that passed.
DETAIL_FIELDS = ("nameWithOwner url description forkCount licenseInfo { spdxId }"
                 " issues(states: OPEN) { totalCount } pullRequests(states: OPEN) { totalCount }")


def repo_query(names: list[str], labels_of: dict[str, list[str]] | None = None) -> str:
    """One GraphQL query for many repositories, one alias each. Without labels_of it asks only for
    what the requirements are checked on. With it, for the details and the count of open issues
    with those labels, which is slow for GitHub, so it is asked only for repositories that passed.
    JSON strings are valid GraphQL strings."""
    parts = []
    for n, name in enumerate(names):
        owner, repo = name.split("/", 1)
        fields = (f"{DETAIL_FIELDS} newcomer: issues(states: OPEN, labels: {json.dumps(labels_of[name])})"
                  " { totalCount }") if labels_of else CHECK_FIELDS
        parts.append(f"r{n}: repository(owner: {json.dumps(owner)}, name: {json.dumps(repo)}) {{ {fields} }}")
    return "query {\n" + "\n".join(parts) + "\n}"


def graphql_record(node: dict) -> dict:
    """A GraphQL repository in the shape repo_record expects."""
    return {
        "full_name": node["nameWithOwner"], "html_url": node["url"], "description": node.get("description"),
        "language": (node.get("primaryLanguage") or {}).get("name"), "stargazers_count": node["stargazerCount"],
        "forks_count": node["forkCount"],
        "open_issues_count": node["issues"]["totalCount"] + node["pullRequests"]["totalCount"],
        "topics": [t["topic"]["name"] for t in node["repositoryTopics"]["nodes"]],
        "license": {"spdx_id": (node.get("licenseInfo") or {}).get("spdxId")},
        "pushed_at": node["pushedAt"], "created_at": node["createdAt"],
    }


def meets_requirements(node: dict, args: argparse.Namespace, today: datetime) -> bool:
    """What the search asks GitHub for, checked here, except the count of newcomer issues."""
    language = (node.get("primaryLanguage") or {}).get("name") or ""
    if language.lower() not in {wanted.lower() for wanted in args.language}:
        return False
    if node["isArchived"] or node["isFork"] or node["isPrivate"]:
        return False
    stars = node["stargazerCount"]
    if stars < args.min_stars or (args.max_stars and stars > args.max_stars):
        return False
    if parse_time(node["pushedAt"]) < today - timedelta(days=args.pushed_days):
        return False
    if parse_time(node["createdAt"]) > today - timedelta(days=args.min_age_days):
        return False
    topics = {t["topic"]["name"].lower() for t in node["repositoryTopics"]["nodes"]}
    return all(topic.lower() in topics for topic in args.topic)


def sort_key(node: dict, sort: str):
    if sort == "stars":
        return node["stargazerCount"]
    if sort == "updated":
        return node["pushedAt"]
    return node["newcomer"]["totalCount"]


def lookup(graphql: GraphQL, names: list[str], labels_of: dict[str, list[str]] | None,
           source: str) -> tuple[list, str | None]:
    """(repositories found, why the lookup stopped early or None). Deleted repositories come back
    as null and are skipped. A batch that fails is skipped too."""
    nodes = []
    for start in range(0, len(names), GRAPHQL_BATCH):
        try:
            answer = graphql(repo_query(names[start:start + GRAPHQL_BATCH], labels_of))
        except RateLimited as e:
            return nodes, str(e)
        except (OSError, http.client.HTTPException, ValueError) as e:
            log(f"{source}: lookup failed ({type(e).__name__}: {e})")
            continue
        nodes += [node for node in (answer.get("data") or {}).values() if node]
    return nodes, None


def labels_for(nodes: list[dict], listed: dict[str, str | None], help_wanted: bool) -> dict[str, list[str]]:
    """The newcomer labels to count in each repository: the common ones, and the project's own.
    Keyed by the name GitHub returned, which may differ from the listed one after a rename."""
    base = NEWCOMER_LABELS + (HELP_WANTED_LABELS if help_wanted else [])
    known = {label.lower() for label in base}
    own = {name.lower(): label for name, label in listed.items() if label and label.lower() not in known}
    return {n["nameWithOwner"]: base + ([own[n["nameWithOwner"].lower()]] if n["nameWithOwner"].lower() in own else [])
            for n in nodes}


def find_in_list(source: str, graphql: GraphQL | None, fetch: Fetch | None, args: argparse.Namespace,
                 today: datetime, repos: dict, totals: dict) -> str | None:
    """Reads a curated list, looks its repositories up, and keeps those in the wanted languages
    that meet the same requirements as the search, best first by --sort."""
    if graphql is None or fetch is None:
        log(f"{source}: skipped, curated lists need a GitHub token")
        return None
    try:
        listed = PARSERS[source](fetch(CURATED_URLS[source]))
    except (OSError, ValueError) as e:
        log(f"{source}: could not read the list ({e})")
        return None
    names = [name for name in listed if name not in repos]
    log(f"{source}: looking up {len(names)} listed repositories")
    nodes, stopped = lookup(graphql, names, None, source)
    nodes = [n for n in nodes if meets_requirements(n, args, today)]
    if nodes and not stopped:
        log(f"{source}: counting newcomer issues in {len(nodes)} that meet the requirements")
        details, stopped = lookup(graphql, [n["nameWithOwner"] for n in nodes],
                                  labels_for(nodes, listed, args.help_wanted), source)
        details = {d["nameWithOwner"]: d for d in details}
        nodes = [{**n, **details[n["nameWithOwner"]]} for n in nodes if n["nameWithOwner"] in details]
        nodes = [n for n in nodes if n["newcomer"]["totalCount"] >= args.min_issues]
    else:
        nodes = []
    for language in args.language:
        picked = [n for n in nodes if n["primaryLanguage"]["name"].lower() == language.lower()]
        totals[f"{language} {source}"] = len(picked)
        picked.sort(key=lambda n: sort_key(n, args.sort), reverse=True)
        found = 0
        for node in picked[:args.per_language]:
            if node["nameWithOwner"] not in repos:
                repos[node["nameWithOwner"]] = repo_record(graphql_record(node), language, source)
                found += 1
        log(f"{language}: {found} repositories from {source}")
    if stopped:
        log(stopped)
    return stopped


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


def make_graphql(token: str) -> GraphQL:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "github-repo-finder",
               "Authorization": f"Bearer {token}"}

    def graphql(query: str) -> dict:
        body = json.dumps({"query": query}).encode("utf-8")
        for attempt in range(2):
            try:
                request = urllib.request.Request(f"{API}/graphql", data=body, headers=headers)
                with urllib.request.urlopen(request, timeout=60) as r:
                    answer = json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                if e.code in (403, 429):
                    raise RateLimited("GitHub rate limit reached") from e
                if e.code < 500 or attempt:
                    raise
                continue
            except (OSError, http.client.HTTPException):   # dropped connection or timeout: try once more
                if attempt:
                    raise
                continue
            if any(err.get("type") == "RATE_LIMITED" for err in answer.get("errors", [])):
                raise RateLimited("GitHub rate limit reached")
            return answer        # NOT_FOUND errors for single repositories are expected
        raise AssertionError("unreachable")
    return graphql


def fetch_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "github-repo-finder"})
    with urllib.request.urlopen(request, timeout=60) as r:
        return r.read().decode("utf-8")


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
    parser.add_argument("--per-language", type=int, default=20,
                        help="repositories per language, for each search label and each curated list")
    parser.add_argument("--source", action="append", choices=SOURCES,
                        help="where to look, repeatable, in this order (default: search). "
                             "goodfirstissue and upforgrabs are curated lists and need a GitHub token")
    args = parser.parse_args()
    args.source = args.source or ["search"]

    token = github_token()
    if not token:
        log("No GitHub token found (GITHUB_TOKEN or `gh auth login`). Search allows 10 requests a minute without one.")
    graphql = make_graphql(token) if token else None
    result = find(make_search(token), args, datetime.now(timezone.utc), graphql, fetch_text)
    sys.stdout.reconfigure(encoding="utf-8")
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
