"""Judge how hard it is to build and test a GitHub repository locally, from its files, its setup
instructions and how long its CI takes, and sort the repositories into easy, moderate and hard."""

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
from datetime import datetime
from pathlib import PurePosixPath
from typing import Callable

API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com"
PAUSE_SECONDS = 0.2
RETRY_SECONDS = 3
MAX_DOCS = 3                 # setup documents read per repository
MAX_EXCERPT_CHARS = 1500
MIN_CI_COMMITS = 3           # fewer PR commits with CI runs than this say nothing about CI time
MARKER_DEPTH = 2             # build files deeper than this belong to examples, tests or vendored code

# Folders whose build files are not part of building the project itself.
IGNORED_DIRS = {"node_modules", "vendor", "third_party", "third-party", "external", "examples", "example",
                "samples", "sample", "docs", "doc", "test", "tests", "testdata", "fixtures", "benchmarks",
                ".github", "contrib"}

# File name -> ecosystem. One ecosystem is one toolchain a contributor has to install.
ECOSYSTEM_FILES = {
    "pyproject.toml": "Python", "setup.py": "Python", "setup.cfg": "Python", "Pipfile": "Python",
    "requirements.txt": "Python",
    "package.json": "JavaScript",
    "pom.xml": "JVM", "build.gradle": "JVM", "build.gradle.kts": "JVM", "build.sbt": "JVM",
    "Cargo.toml": "Rust",
    "go.mod": "Go",
    "CMakeLists.txt": "C/C++", "meson.build": "C/C++", "configure.ac": "C/C++", "Makefile.am": "C/C++",
    "Gemfile": "Ruby",
    "composer.json": "PHP",
    "pubspec.yaml": "Dart",
    "mix.exs": "Elixir",
}
ECOSYSTEM_SUFFIXES = {".csproj": ".NET", ".sln": ".NET", ".fsproj": ".NET"}

# Root file -> build tool, in the order they are shown.
ROOT_TOOLS = [
    ("mvnw", "Maven (wrapper included)"), ("pom.xml", "Maven"),
    ("gradlew", "Gradle (wrapper included)"), ("build.gradle", "Gradle"), ("build.gradle.kts", "Gradle"),
    ("uv.lock", "uv"), ("poetry.lock", "Poetry"), ("pdm.lock", "PDM"), ("tox.ini", "tox"), ("noxfile.py", "nox"),
    ("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "Yarn"), ("package-lock.json", "npm"), ("bun.lockb", "Bun"),
    ("Cargo.toml", "Cargo"), ("go.mod", "Go modules"),
    ("CMakeLists.txt", "CMake"), ("meson.build", "Meson"), ("configure.ac", "Autotools"),
    ("WORKSPACE", "Bazel"), ("WORKSPACE.bazel", "Bazel"), ("MODULE.bazel", "Bazel"),
    ("Makefile", "Make"), ("justfile", "just"), ("Taskfile.yml", "Task"),
]
# Build systems that are hard to get working on a laptop for a first contribution.
HEAVY_TOOLS = {"Bazel", "Autotools"}
# Tools that hide the toolchain behind a wrapper: one fewer thing to install.
WRAPPED = {"Maven (wrapper included)", "Gradle (wrapper included)"}

# A document extension is required: BUILD is a Bazel file and setup.py is code.
SETUP_DOC = re.compile(r"^(development|developing|hacking|building|build|contributing|install|setup|"
                       r"getting[-_ ]started|readme)([-_][\w-]+)?\.(md|markdown|rst|txt|adoc)$", re.IGNORECASE)
SETUP_DOC_DIRS = {"", ".github", "docs", "doc", "docs/development", "docs/contributing", "docs/dev"}
# Headings of sections that explain how to set up a development environment.
SETUP_HEADING = re.compile(r"(set ?up|development environment|dev environment|local development|getting started"
                           r"|build(ing)? (from source|locally|the project)|^building\b|^build\b|install(ing)? "
                           r"(from source|for development|dependencies)|run(ning)? (the )?(unit |the unit )?tests"
                           r"|testing|prerequisites|requirements)", re.IGNORECASE)
MD_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$", re.MULTILINE)
# reStructuredText: a title line underlined (and maybe overlined) with one repeated punctuation mark.
RST_HEADING = re.compile(r"^(\S[^\n]*)\n([=\-~^\"'`#*+])\2{2,}[ \t]*$", re.MULTILINE)
FENCE = re.compile(r"^(```|~~~)[^\n]*\n(.*?)^\1", re.MULTILINE | re.DOTALL)
NOT_A_COMMAND = re.compile(r"^\s*($|#|//|>|\.\.\.|[\[{(<]|[A-Z][a-z]+:)")

Get = Callable[[str], object]       # API path -> parsed JSON, raises NotFound for 404
GetText = Callable[[str], str]      # raw file URL -> text, raises NotFound for 404


class NotFound(Exception):
    pass


class RateLimited(Exception):
    pass


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


# ---- what the files say -------------------------------------------------------------------------

def counted(path: str) -> bool:
    """Whether a build file at this path is part of building the project itself."""
    parts = PurePosixPath(path).parts
    return len(parts) <= MARKER_DEPTH and not any(part.lower() in IGNORED_DIRS for part in parts[:-1])


def ecosystems(paths: list[str]) -> list[str]:
    found = []
    for path in paths:
        if not counted(path):
            continue
        name = PurePosixPath(path).name
        kind = ECOSYSTEM_FILES.get(name) or ECOSYSTEM_SUFFIXES.get(PurePosixPath(name).suffix)
        if kind and kind not in found:
            found.append(kind)
    return found


def build_tools(paths: list[str]) -> list[str]:
    """Build tools named by files in the root. A wrapper replaces the tool it wraps."""
    root = {path for path in paths if "/" not in path}
    tools = []
    for file, tool in ROOT_TOOLS:
        base = tool.removesuffix(" (wrapper included)")
        if file in root and tool not in tools and not any(t.startswith(base) for t in tools):
            tools.append(tool)
    return tools


def file_facts(paths: list[str]) -> dict:
    names = set(paths)
    root = {path for path in paths if "/" not in path}
    return {
        "devcontainer": ".devcontainer.json" in names or any(p.startswith(".devcontainer/") and
                                                             p.endswith("devcontainer.json") for p in paths),
        "gitpod": ".gitpod.yml" in names,
        "docker": any(re.match(r"^(dockerfile|(docker-)?compose\.ya?ml)$", p, re.IGNORECASE) for p in root),
        "nix": "flake.nix" in root or "shell.nix" in root,
        "gpu": any(p.endswith((".cu", ".cuh")) for p in paths),
        "build_tools": build_tools(paths),
        "ecosystems": ecosystems(paths),
    }


def setup_docs(paths: list[str]) -> list[str]:
    """Files that may explain the setup, the most specific first: a development guide before the
    contributing guide, and both before the README."""
    def rank(path):
        name = PurePosixPath(path).name.lower()
        order = ["development", "developing", "hacking", "building", "build", "setup", "getting",
                 "contributing", "install", "readme"]
        return next((n for n, word in enumerate(order) if name.startswith(word)), len(order)), path.count("/")
    def folder(path):
        parent = str(PurePosixPath(path).parent)
        return "" if parent == "." else parent.lower()
    found = [p for p in paths if folder(p) in SETUP_DOC_DIRS and SETUP_DOC.match(PurePosixPath(p).name)]
    return sorted(found, key=rank)[:MAX_DOCS]


def sections(text: str) -> list[tuple[str, int, str]]:
    """(heading, level, body) for each section of a Markdown or reStructuredText document.
    Lines inside code blocks are not headings, though a shell comment starts with #. In
    reStructuredText the level is the order in which each underline mark first appears."""
    fences = [m.span() for m in FENCE.finditer(text)]

    def in_code(pos):
        return any(start <= pos < end for start, end in fences)

    marks = [(m.start(), m.end(), m.group(2), len(m.group(1)))
             for m in MD_HEADING.finditer(text) if not in_code(m.start())]
    if not marks:
        order: list[str] = []
        for m in RST_HEADING.finditer(text):
            if set(m.group(1).strip()) <= set(m.group(2)):   # an overline, not a title
                continue
            if m.group(2) not in order:
                order.append(m.group(2))
            marks.append((m.start(), m.end(), m.group(1), order.index(m.group(2)) + 1))
    marks.sort()
    out = []
    for n, (start, end, title, level) in enumerate(marks):
        stop = marks[n + 1][0] if n + 1 < len(marks) else len(text)
        out.append((title.strip(), level, text[end:stop]))
    return out


def setup_section(text: str) -> str | None:
    """The text right under each heading about setting up, building or testing, joined. Subsections
    count only when their own heading is about that too: a guide often puts everything, optional
    database and documentation tooling included, under one "Development environment" heading.
    None if there is no such heading."""
    picked = [f"{title}\n{body.strip()}" for title, _, body in sections(text) if SETUP_HEADING.search(title)]
    return "\n\n".join(picked) if picked else None


def shell_commands(text: str) -> int:
    """Lines in code blocks that look like commands to run. Comments, output and code are mostly skipped."""
    count = 0
    for block in FENCE.finditer(text):
        for line in block.group(2).splitlines():
            line = line.strip().removeprefix("$ ")
            if not NOT_A_COMMAND.match(line) and not line.endswith((";", "{", "}", ",")):
                count += 1
    # Outside fenced blocks, as in reStructuredText or indented blocks, only "$ make" lines are sure.
    rest = FENCE.sub("", text)
    return count + sum(1 for line in rest.splitlines() if line.strip().startswith("$ "))


# ---- CI -----------------------------------------------------------------------------------------

def ci_minutes(runs: list[dict]) -> tuple[float | None, int]:
    """(median minutes until every workflow of a PR commit finished, how many commits measured).
    A contributor waits for the slowest workflow, so each commit counts its longest run."""
    longest: dict[str, float] = {}
    for run in runs:
        if not run.get("run_started_at") or not run.get("updated_at"):
            continue
        minutes = (parse_time(run["updated_at"]) - parse_time(run["run_started_at"])).total_seconds() / 60
        sha = run.get("head_sha") or run.get("id")
        longest[sha] = max(longest.get(sha, 0), minutes)
    if len(longest) < MIN_CI_COMMITS:
        return None, len(longest)
    return round(statistics.median(longest.values()), 1), len(longest)


# ---- judging ------------------------------------------------------------------------------------

def rate(facts: dict, limits: argparse.Namespace) -> tuple[str, list[str]]:
    """(easy, moderate or hard, the reasons). Each difficulty adds points, each help takes one off."""
    points, reasons = 0, []

    def add(n, reason):
        nonlocal points
        points += n
        reasons.append(reason)

    kinds = facts["ecosystems"]
    if len(kinds) >= 3:
        add(2, f"{len(kinds)} toolchains to install: {', '.join(kinds)}")
    elif len(kinds) == 2:
        add(1, f"two toolchains to install: {', '.join(kinds)}")
    heavy = [t for t in facts["build_tools"] if t in HEAVY_TOOLS]
    if heavy:
        add(2, f"built with {', '.join(heavy)}, which is slow to set up")
    if facts["gpu"]:
        add(1, "has CUDA code, which may need a GPU")
    if not facts["setup_doc"]:
        add(1, "no setup instructions in the repository" +
            (f"; {facts['docs_read'][0]} may link to them elsewhere" if facts["docs_read"] else ""))
    elif facts["setup_commands"] > limits.max_setup_commands:
        add(1, f"setup instructions have {facts['setup_commands']} commands")
    ci = facts["ci_median_minutes"]
    if ci is not None and ci > 2 * limits.max_ci_minutes:
        add(2, f"CI takes {ci:g} minutes per PR commit (median)")
    elif ci is not None and ci > limits.max_ci_minutes:
        add(1, f"CI takes {ci:g} minutes per PR commit (median)")
    if facts["size_mb"] > limits.max_size_mb:
        add(1, f"the repository is {facts['size_mb'] / 1024:.1f} GB to clone")
    if facts["devcontainer"] or facts["gitpod"]:
        add(-1, "has a ready development container" if facts["devcontainer"] else "has a Gitpod setup")
    if any(t in WRAPPED for t in facts["build_tools"]):
        add(-1, "the build tool comes with the repository")

    if points >= 3:
        return "hard", reasons
    if points >= 1:
        return "moderate", reasons
    if not reasons:
        reasons.append(f"one toolchain ({', '.join(kinds) or 'none found'}), setup documented")
    return "easy", reasons


# ---- per repository -----------------------------------------------------------------------------

def check_repo(get: Get, get_text: GetText, name: str, limits: argparse.Namespace) -> dict:
    repo = get(f"/repos/{name}")
    branch = repo["default_branch"]
    tree = get(f"/repos/{name}/git/trees/{urllib.parse.quote(branch, safe='')}?recursive=1")
    paths = [entry["path"] for entry in tree.get("tree", []) if entry.get("type") == "blob"]
    facts = {"default_branch": branch, "size_mb": round(repo.get("size", 0) / 1024, 1),
             "files_listed": len(paths), "tree_truncated": bool(tree.get("truncated")), **file_facts(paths)}

    facts.update(docs_read=[], setup_doc=None, setup_commands=0, setup_excerpt="")
    for path in setup_docs(paths):
        try:
            text = get_text(f"{RAW}/{name}/{urllib.parse.quote(branch)}/{urllib.parse.quote(path)}")
        except (NotFound, OSError):
            continue
        facts["docs_read"].append(path)
        section = setup_section(text)
        if section:
            facts.update(setup_doc=path, setup_commands=shell_commands(section),
                         setup_excerpt=section[:MAX_EXCERPT_CHARS])
            break

    try:
        runs = get(f"/repos/{name}/actions/runs?event=pull_request&status=success&per_page=100")
        facts["ci_median_minutes"], facts["ci_commits"] = ci_minutes(runs.get("workflow_runs", []))
    except NotFound:                      # Actions disabled, or no access
        facts["ci_median_minutes"], facts["ci_commits"] = None, 0

    rating, reasons = rate(facts, limits)
    return {"rating": rating, "reasons": reasons, "facts": facts}


def entries_from(data) -> list:
    """A JSON array of names or objects with full_name, or the object written by github-repo-finder
    (repos), repo-health-checker (healthy) or rule-filter (kept)."""
    if isinstance(data, dict):
        for key in ("repos", "healthy", "kept"):
            if key in data:
                return data[key]
        raise ValueError('expected a JSON array or an object with "repos", "healthy" or "kept"')
    return data


def repo_name(entry, field: str) -> str | None:
    if isinstance(entry, str):
        return entry
    return entry.get(field) or (entry.get("item") or {}).get(field)


def check_all(entries: list, get: Get, get_text: GetText, limits: argparse.Namespace) -> dict:
    result = {"easy": [], "moderate": [], "hard": [], "unknown": []}
    stop_reason = None
    for n, entry in enumerate(entries, 1):
        name = repo_name(entry, limits.repo_field)
        if stop_reason:
            setup = {"rating": "unknown", "reasons": [stop_reason]}
        elif not name:
            setup = {"rating": "unknown", "reasons": [f'no "{limits.repo_field}" field']}
        else:
            try:
                setup = check_repo(get, get_text, name, limits)
            except NotFound:
                setup = {"rating": "unknown", "reasons": ["repository not found"]}
            except RateLimited as e:
                stop_reason = str(e)
                setup = {"rating": "unknown", "reasons": [stop_reason]}
            except OSError as e:          # network errors, including HTTP errors
                setup = {"rating": "unknown", "reasons": [f"GitHub request failed ({e})"]}
        base = {"full_name": entry} if isinstance(entry, str) else entry
        result[setup["rating"]].append({**base, "setup": setup})
        log(f"{n}/{len(entries)} {setup['rating']}: {name} - {'; '.join(setup['reasons'])}")
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
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "dev-setup-checker",
               "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    def get(path: str):
        request = urllib.request.Request(API + path, headers=headers)
        for attempt in range(2):
            time.sleep(PAUSE_SECONDS)
            try:
                with urllib.request.urlopen(request, timeout=60) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                if e.code in (404, 409, 451):        # 409: empty repository
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
    request = urllib.request.Request(url, headers={"User-Agent": "dev-setup-checker"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise NotFound(url) from e
        raise


# ---- command line -------------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repos", nargs="*", help="owner/name; without these, JSON is read from stdin")
    parser.add_argument("--repo-field", default="full_name")
    parser.add_argument("--max-setup-commands", type=int, default=15,
                        help="more commands than this in the setup instructions is a difficulty")
    parser.add_argument("--max-ci-minutes", type=float, default=30,
                        help="CI slower than this is a difficulty, twice as slow a bigger one")
    parser.add_argument("--max-size-mb", type=float, default=1024, help="a bigger repository is slow to clone")
    args = parser.parse_args()

    entries = args.repos or entries_from(json.loads(sys.stdin.buffer.read().decode("utf-8-sig")))
    token = github_token()
    if not token:
        log("No GitHub token found (GITHUB_TOKEN or `gh auth login`). Without one only 60 requests an hour are allowed.")
    result = check_all(entries, make_get(token), get_text, args)
    sys.stdout.reconfigure(encoding="utf-8")
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
