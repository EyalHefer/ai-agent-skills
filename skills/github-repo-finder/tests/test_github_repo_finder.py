import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import jsonschema
import pytest

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL / "scripts"))

from github_repo_finder import (PER_PAGE, RateLimited, build_query, find, parse_goodfirstissue,  # noqa: E402
                                parse_upforgrabs, repo_record)

TODAY = datetime(2026, 11, 3, tzinfo=timezone.utc)


def options(**changes):
    values = dict(language=["Python"], min_stars=1000, max_stars=None, pushed_days=30, min_age_days=365,
                  min_issues=1, help_wanted=False, topic=[], sort="updated", per_language=20, source=["search"])
    return argparse.Namespace(**{**values, **changes})


def item(name, language="Python", stars=1500):
    return {"full_name": name, "html_url": f"https://github.com/{name}", "description": None,
            "language": language, "stargazers_count": stars, "forks_count": 10, "open_issues_count": 7,
            "topics": ["cli"], "license": {"spdx_id": "MIT"}, "pushed_at": "2026-11-01T10:00:00Z",
            "created_at": "2019-01-01T10:00:00Z", "owner": {"login": name.split("/")[0]}}


class FakeSearch:
    """Answers each query from a list of repos, one page at a time."""

    def __init__(self, results: dict[str, list[dict]], fail_after=None):
        self.results, self.queries, self.fail_after = results, [], fail_after

    def __call__(self, params: dict) -> dict:
        if self.fail_after is not None and len(self.queries) >= self.fail_after:
            raise RateLimited("GitHub search limit reached, try again in 60 seconds")
        self.queries.append(params)
        everything = next((v for k, v in self.results.items() if k in params["q"]), [])
        start = (params["page"] - 1) * params["per_page"]
        return {"total_count": len(everything), "items": everything[start:start + params["per_page"]]}


def test_query_has_every_requirement():
    query = build_query("C++", options(), TODAY, "good-first-issues")
    assert query.split(" ") == ['language:"C++"', "stars:>=1000", "pushed:>=2026-10-04", "created:<=2025-11-03",
                                "good-first-issues:>=1", "archived:false", "is:public", "fork:false"]


def test_query_with_star_range_and_topics():
    query = build_query("Java", options(max_stars=50000, topic=["testing", "cli"]), TODAY, "help-wanted-issues")
    assert "stars:1000..50000" in query and "help-wanted-issues:>=1" in query
    assert 'topic:"testing" topic:"cli"' in query


def test_record_keeps_the_fields_later_skills_need():
    record = repo_record(item("acme/widgets"), "Python")
    assert record["full_name"] == "acme/widgets" and record["description"] == ""
    assert record["license"] == "MIT" and "owner" not in record


def test_one_search_per_language_and_duplicates_kept_once():
    search = FakeSearch({'"Python"': [item("acme/a"), item("acme/shared")],
                         '"Java"': [item("acme/shared", "Java"), item("acme/b", "Java")]})
    result = find(search, options(language=["Python", "Java"]), TODAY)
    assert [r["full_name"] for r in result["repos"]] == ["acme/a", "acme/shared", "acme/b"]
    assert result["totals"] == {"Python good-first-issues": 2, "Java good-first-issues": 2}
    assert len(search.queries) == 2


def test_help_wanted_adds_a_second_search():
    search = FakeSearch({"good-first": [item("acme/a")], "help-wanted": [item("acme/b")]})
    result = find(search, options(help_wanted=True), TODAY)
    assert [r["full_name"] for r in result["repos"]] == ["acme/a", "acme/b"]


def test_reads_more_pages_until_the_limit():
    many = [item(f"acme/r{n}") for n in range(250)]
    search = FakeSearch({"Python": many})
    result = find(search, options(per_language=150), TODAY)
    assert len(result["repos"]) == 150
    assert [q["page"] for q in search.queries] == [1, 2]


def test_stops_when_results_end():
    search = FakeSearch({"Python": [item("acme/a")]})
    find(search, options(per_language=PER_PAGE * 3), TODAY)
    assert len(search.queries) == 1


def test_rate_limit_returns_what_was_found_so_far():
    search = FakeSearch({"Python": [item("acme/a")], "Java": [item("acme/b")]}, fail_after=1)
    result = find(search, options(language=["Python", "Java"]), TODAY)
    assert [r["full_name"] for r in result["repos"]] == ["acme/a"]
    assert result["stopped"].startswith("GitHub search limit")


def test_output_matches_the_schema():
    result = find(FakeSearch({"Python": [item("acme/a")]}), options(), TODAY)
    schema = json.loads((SKILL / "schema.json").read_text(encoding="utf-8"))["output"]
    jsonschema.validate(result, schema)


# ---- curated lists ----

def node(name, language="Python", stars=1500, newcomer=3, pushed="2026-11-01T10:00:00Z",
         created="2019-01-01T10:00:00Z", archived=False, topics=("cli",)):
    return {"nameWithOwner": name, "url": f"https://github.com/{name}", "description": None,
            "stargazerCount": stars, "forkCount": 4, "pushedAt": pushed, "createdAt": created,
            "isArchived": archived, "isFork": False, "isPrivate": False,
            "primaryLanguage": {"name": language} if language else None, "licenseInfo": None,
            "repositoryTopics": {"nodes": [{"topic": {"name": t}} for t in topics]},
            "issues": {"totalCount": 9}, "pullRequests": {"totalCount": 2}, "newcomer": {"totalCount": newcomer}}


class FakeGraphQL:
    """Answers each aliased repository from a dict of nodes; unknown names come back null."""

    def __init__(self, nodes):
        self.nodes, self.queries = {n["nameWithOwner"]: n for n in nodes}, []

    def __call__(self, query):
        self.queries.append(query)

        data = {}
        for alias, owner, name in re.findall(r'(r\d+): repository\(owner: "([^"]+)", name: "([^"]+)"\)', query):
            data[alias] = self.nodes.get(f"{owner}/{name}")
        return {"data": data}


TOML = """repositories = [
  'github.com/acme/listed',
  'github.com/acme/old-news',
  'github.com/acme/gone',
]"""


def fetch_from(texts):
    return lambda url: texts[next(k for k in texts if k in url)]


def test_parse_goodfirstissue():
    assert list(parse_goodfirstissue(TOML)) == ["acme/listed", "acme/old-news", "acme/gone"]


def test_parse_upforgrabs_keeps_the_projects_own_label():
    text = json.dumps({
        "a": {"site": "https://github.com/acme/tool", "upforgrabs": {"name": "E-easy", "link": "x"}},
        "b": {"site": "https://acme.dev", "upforgrabs": {"name": "beginner",
                                                         "link": "https://github.com/acme/site/labels/beginner"}},
        "c": {"site": "https://gitlab.com/acme/other", "upforgrabs": {"name": "easy", "link": "https://gitlab.com/x"}},
    })
    assert parse_upforgrabs(text) == {"acme/tool": "E-easy", "acme/site": "beginner"}


def test_curated_list_applies_the_same_requirements_as_search():
    graphql = FakeGraphQL([node("acme/listed"), node("acme/old-news", pushed="2026-01-01T00:00:00Z")])
    result = find(FakeSearch({}), options(source=["goodfirstissue"]), TODAY, graphql,
                  fetch_from({"good-first-issue": TOML}))
    assert [(r["full_name"], r["source"]) for r in result["repos"]] == [("acme/listed", "goodfirstissue")]
    assert result["totals"] == {"Python goodfirstissue": 1}
    assert result["stopped"] is None


@pytest.mark.parametrize("changes", [dict(language="Java"), dict(stars=500), dict(newcomer=0),
                                     dict(archived=True), dict(created="2026-06-01T00:00:00Z"), dict(language=None)])
def test_curated_repo_is_dropped_when_a_requirement_fails(changes):
    graphql = FakeGraphQL([node("acme/listed", **changes)])
    result = find(FakeSearch({}), options(source=["goodfirstissue"]), TODAY, graphql,
                  fetch_from({"good-first-issue": TOML}))
    assert result["repos"] == []


def test_upforgrabs_label_is_counted_as_a_newcomer_label():
    text = json.dumps({"a": {"site": "https://github.com/acme/tool", "upforgrabs": {"name": "up-for-grabs"}}})
    graphql = FakeGraphQL([node("acme/tool")])
    find(FakeSearch({}), options(source=["upforgrabs"]), TODAY, graphql, fetch_from({"up-for-grabs": text}))
    counting = graphql.queries[-1]
    assert '"up-for-grabs"' in counting and '"beginner"' in counting
    assert "newcomer" not in graphql.queries[0]      # the slow count is asked only for repos that passed


def test_sources_are_combined_and_a_repo_is_kept_once():
    search = FakeSearch({"Python": [item("acme/listed")]})
    graphql = FakeGraphQL([node("acme/listed"), node("acme/extra")])
    toml = "'github.com/acme/listed',\n'github.com/acme/extra'"
    result = find(search, options(source=["search", "goodfirstissue"]), TODAY, graphql,
                  fetch_from({"good-first-issue": toml}))
    assert [(r["full_name"], r["source"]) for r in result["repos"]] == [("acme/listed", "search"),
                                                                         ("acme/extra", "goodfirstissue")]
    assert "acme/listed" not in graphql.queries[0]      # already found, not looked up again


def test_curated_sort_and_limit():
    graphql = FakeGraphQL([node("acme/a", stars=2000), node("acme/b", stars=9000), node("acme/c", stars=5000)])
    toml = "'github.com/acme/a' 'github.com/acme/b' 'github.com/acme/c'"
    result = find(FakeSearch({}), options(source=["goodfirstissue"], sort="stars", per_language=2), TODAY,
                  graphql, fetch_from({"good-first-issue": toml}))
    assert [r["full_name"] for r in result["repos"]] == ["acme/b", "acme/c"]


def test_unreadable_list_is_skipped():
    def fail(url):
        raise OSError("timed out")
    result = find(FakeSearch({"Python": [item("acme/a")]}), options(source=["upforgrabs", "search"]), TODAY,
                  FakeGraphQL([]), fail)
    assert [r["full_name"] for r in result["repos"]] == ["acme/a"]


def test_curated_lists_without_a_token_are_skipped():
    result = find(FakeSearch({}), options(source=["goodfirstissue"]), TODAY, None, None)
    assert result == {"repos": [], "totals": {}, "stopped": None}


def test_curated_output_matches_the_schema():
    result = find(FakeSearch({}), options(source=["goodfirstissue"]), TODAY, FakeGraphQL([node("acme/listed")]),
                  fetch_from({"good-first-issue": TOML}))
    schema = json.loads((SKILL / "schema.json").read_text(encoding="utf-8"))["output"]
    jsonschema.validate(result, schema)
