import argparse
import json
import sys
from pathlib import Path

import jsonschema
import pytest

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL / "scripts"))

from dev_setup_checker import (NotFound, RateLimited, build_tools, check_all, ci_minutes,  # noqa: E402
                               ecosystems, entries_from, file_facts, rate, sections, setup_docs,
                               setup_section, shell_commands)

LIMITS = argparse.Namespace(repo_field="full_name", max_setup_commands=15, max_ci_minutes=30, max_size_mb=1024)


# ---- files ----

def test_ecosystems_from_build_files_of_the_project_only():
    paths = ["pyproject.toml", "frontend/package.json", "src/lib/deep/Cargo.toml", "examples/go.mod",
             "docs/package.json", "node_modules/x/package.json", "app/App.csproj"]
    assert ecosystems(paths) == ["Python", "JavaScript", ".NET"]


def test_build_tools_and_wrappers():
    assert build_tools(["gradlew", "build.gradle", "package-lock.json", "Makefile", "sub/pom.xml"]) == [
        "Gradle (wrapper included)", "npm", "Make"]
    assert build_tools(["pom.xml", "WORKSPACE"]) == ["Maven", "Bazel"]


def test_file_facts():
    facts = file_facts([".devcontainer/devcontainer.json", "docker-compose.yml", "kernels/add.cu", "flake.nix"])
    assert (facts["devcontainer"], facts["docker"], facts["gpu"], facts["nix"], facts["gitpod"]) == (
        True, True, True, True, False)


def test_setup_docs_most_specific_first_and_never_code():
    paths = ["README.md", "CONTRIBUTING.md", "docs/development.md", "setup.py", "BUILD", "src/README.md",
             ".github/CONTRIBUTING.md", "INSTALL.rst"]
    assert setup_docs(paths) == ["docs/development.md", "CONTRIBUTING.md", ".github/CONTRIBUTING.md"]


# ---- documents ----

GUIDE = """# Contributing

Thanks!

## Development setup

```bash
# get the code
git clone https://github.com/acme/widgets
cd widgets
pip install -e ".[dev]"
```

### Optional: docs

```bash
make docs
```

## Running the tests

```
$ pytest
```

## Code of conduct

Be kind.
"""


def test_sections_and_their_levels():
    assert [(title, level) for title, level, _ in sections(GUIDE)] == [
        ("Contributing", 1), ("Development setup", 2), ("Optional: docs", 3), ("Running the tests", 2),
        ("Code of conduct", 2)]


def test_comments_in_code_blocks_are_not_headings():
    assert [title for title, _, _ in sections(GUIDE)].count("get the code") == 0


def test_setup_section_takes_the_text_right_under_setup_headings():
    section = setup_section(GUIDE)
    assert section.startswith("Development setup\n```bash")
    assert "pytest" in section and "make docs" not in section and "Be kind" not in section


def test_commands_are_counted_without_comments():
    assert shell_commands(setup_section(GUIDE)) == 4


def test_restructuredtext():
    text = """Contributing
============

First time setup
----------------

.. code-block:: text

    $ git clone https://github.com/acme/widgets
    $ pip install -e .

Running the tests
~~~~~~~~~~~~~~~~~

    $ pytest
"""
    assert [(t, lv) for t, lv, _ in sections(text)] == [("Contributing", 1), ("First time setup", 2),
                                                       ("Running the tests", 3)]
    assert shell_commands(setup_section(text)) == 3


def test_no_setup_heading():
    assert setup_section("# Contributing\n\nSee https://acme.dev/contributing.") is None


# ---- CI ----

def run(sha, start, end):
    return {"head_sha": sha, "run_started_at": f"2026-11-01T10:{start:02d}:00Z",
            "updated_at": f"2026-11-01T10:{end:02d}:00Z"}


def test_ci_takes_the_slowest_workflow_of_each_commit():
    runs = [run("a", 0, 5), run("a", 0, 20), run("b", 0, 10), run("c", 0, 30), run("c", 0, 2)]
    assert ci_minutes(runs) == (20.0, 3)


def test_too_few_commits_say_nothing():
    assert ci_minutes([run("a", 0, 50), run("b", 0, 50)]) == (None, 2)


# ---- rating ----

def facts(**changes):
    values = dict(ecosystems=["Python"], build_tools=["uv"], gpu=False, devcontainer=False, gitpod=False,
                  setup_doc="CONTRIBUTING.md", docs_read=["CONTRIBUTING.md"], setup_commands=4,
                  ci_median_minutes=8.0, size_mb=20.0)
    return {**values, **changes}


def test_simple_project_is_easy():
    assert rate(facts(), LIMITS) == ("easy", ["one toolchain (Python), setup documented"])


def test_two_toolchains_are_moderate():
    assert rate(facts(ecosystems=["Python", "Rust"]), LIMITS) == (
        "moderate", ["two toolchains to install: Python, Rust"])


def test_many_difficulties_are_hard():
    rating, reasons = rate(facts(ecosystems=["Python", "Go", "JVM"], ci_median_minutes=70.0), LIMITS)
    assert rating == "hard"
    assert reasons == ["3 toolchains to install: Python, Go, JVM", "CI takes 70 minutes per PR commit (median)"]


def test_bazel_is_heavy():
    assert rate(facts(build_tools=["Bazel"], setup_commands=30), LIMITS)[0] == "hard"


def test_a_devcontainer_or_a_wrapper_makes_it_easier():
    assert rate(facts(ecosystems=["JVM", "JavaScript"], build_tools=["Gradle (wrapper included)"]), LIMITS)[0] == "easy"
    assert rate(facts(setup_doc=None, devcontainer=True), LIMITS)[0] == "easy"


def test_missing_instructions_say_where_they_may_be():
    _, reasons = rate(facts(setup_doc=None, docs_read=["docs/contributing.rst"]), LIMITS)
    assert reasons == ["no setup instructions in the repository; docs/contributing.rst may link to them elsewhere"]


# ---- whole run against a fake GitHub ----

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


def widgets_routes(name="acme/widgets"):
    tree = [{"path": p, "type": "blob"} for p in ("pyproject.toml", "uv.lock", "CONTRIBUTING.md", "README.md",
                                                  "src/widgets/__init__.py")]
    return {
        f"/repos/{name}/git/trees/main": {"tree": tree, "truncated": False},
        f"/repos/{name}/actions/runs": {"workflow_runs": [run(s, 0, 6) for s in "abcd"]},
        f"/repos/{name}": {"default_branch": "main", "size": 2048},
    }


def fake_text(url):
    if url.endswith("CONTRIBUTING.md"):
        return GUIDE
    raise NotFound(url)


def test_whole_run_matches_the_schema():
    get = FakeGitHub(widgets_routes())
    result = check_all(["acme/widgets", {"full_name": "acme/missing", "stars": 10}], get, fake_text, LIMITS)
    schema = json.loads((SKILL / "schema.json").read_text(encoding="utf-8"))["output"]
    jsonschema.validate(result, schema)
    [entry] = result["easy"]
    facts_ = entry["setup"]["facts"]
    assert (facts_["setup_doc"], facts_["setup_commands"], facts_["ci_median_minutes"], facts_["size_mb"]) == (
        "CONTRIBUTING.md", 4, 6.0, 2.0)
    assert facts_["build_tools"] == ["uv"] and facts_["ecosystems"] == ["Python"]
    assert result["unknown"][0]["full_name"] == "acme/missing"
    assert result["unknown"][0]["setup"]["reasons"] == ["repository not found"]


def test_ci_unavailable_is_not_a_failure():
    routes = widgets_routes()
    routes["/repos/acme/widgets/actions/runs"] = NotFound("disabled")
    [entry] = check_all(["acme/widgets"], FakeGitHub(routes), fake_text, LIMITS)["easy"]
    assert entry["setup"]["facts"]["ci_median_minutes"] is None


def test_rate_limit_stops_the_run_but_keeps_every_repo():
    routes = {"/repos/": RateLimited("GitHub rate limit reached")}
    result = check_all(["acme/a", "acme/b"], FakeGitHub(routes), fake_text, LIMITS)
    assert [e["full_name"] for e in result["unknown"]] == ["acme/a", "acme/b"]


@pytest.mark.parametrize("data", [{"repos": [{"full_name": "a/b"}]}, {"healthy": [{"full_name": "a/b"}]},
                                  {"kept": [{"full_name": "a/b"}]}, [{"full_name": "a/b"}]])
def test_accepts_finder_health_and_filter_output(data):
    assert entries_from(data) == [{"full_name": "a/b"}]
