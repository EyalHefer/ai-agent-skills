import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import jsonschema

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL / "scripts"))

from github_repo_finder import PER_PAGE, RateLimited, build_query, find, repo_record  # noqa: E402

TODAY = datetime(2026, 11, 3, tzinfo=timezone.utc)


def options(**changes):
    values = dict(language=["Python"], min_stars=1000, max_stars=None, pushed_days=30, min_age_days=365,
                  min_issues=1, help_wanted=False, topic=[], sort="updated", per_language=20)
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
