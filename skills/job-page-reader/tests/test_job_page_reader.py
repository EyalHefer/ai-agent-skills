import json
import sys
from pathlib import Path

import jsonschema
import pytest

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL / "scripts"))

from job_page_reader import Robots, classify, entries_from, read_pages, url_of  # noqa: E402

JOB_TEXT = "Junior Automation Engineer. Requirements: B.Sc. in Computer Science, Python, Selenium. " * 5
JOB_URL = "https://jobs.example.com/acme/123/junior-automation-engineer"


def test_job_with_description_is_open():
    assert classify(JOB_URL, JOB_URL, 200, JOB_TEXT)[0] == "open"


@pytest.mark.parametrize("notice", [
    "This job is no longer available",
    "Sorry, position is missing",
    "המשרה הזו כבר לא פתוחה",
])
def test_closed_notice_wins_even_when_the_old_description_is_still_shown(notice):
    status, reason = classify(JOB_URL, JOB_URL, 200, notice + "\n" + JOB_TEXT)
    assert status == "closed"
    assert reason.startswith("page says")


def test_redirect_to_company_job_list_means_closed():
    assert classify(JOB_URL, "https://jobs.example.com/acme", 200, JOB_TEXT)[0] == "closed"


def test_redirect_to_another_form_of_the_same_job_is_not_closed():
    final = "https://jobs.example.com/he/acme/123/junior-automation-engineer?ref=1"
    assert classify(JOB_URL, final, 200, JOB_TEXT)[0] == "open"


@pytest.mark.parametrize("code", [404, 410])
def test_missing_page_is_closed(code):
    assert classify(JOB_URL, JOB_URL, code, "Not found")[0] == "closed"


def test_server_error_is_unknown_not_closed():
    assert classify(JOB_URL, JOB_URL, 503, "Service unavailable")[0] == "unknown"


def test_blank_page_is_unknown():
    assert classify(JOB_URL, JOB_URL, 200, "Loading...")[0] == "unknown"


def test_accepts_array_and_results_of_other_skills():
    jobs = [{"url": "a"}]
    assert entries_from(jobs) == jobs
    assert entries_from({"kept": jobs, "rejected": []}) == jobs
    scored = [{"item": {"url": "a"}, "score": 90}]
    assert entries_from({"scored": scored, "unscored": []}) == scored
    with pytest.raises(ValueError):
        entries_from({"other": []})


def test_url_is_found_directly_or_inside_item():
    assert url_of({"url": "a"}, "url") == "a"
    assert url_of({"item": {"url": "b"}, "score": 1}, "url") == "b"
    assert url_of({"title": "no link"}, "url") is None


def test_reads_real_pages_in_a_browser(tmp_path):
    pytest.importorskip("playwright")
    open_page = tmp_path / "open.html"
    open_page.write_text(f"<html><body><h1>Job</h1><p>{JOB_TEXT}</p></body></html>", encoding="utf-8")
    closed_page = tmp_path / "closed.html"
    closed_page.write_text("<html><body>This job is no longer available</body></html>", encoding="utf-8")
    entries = [
        {"item": {"url": open_page.as_uri(), "title": "A"}, "score": 90},
        {"item": {"url": closed_page.as_uri(), "title": "B"}, "score": 80},
        {"item": {"title": "C"}, "score": 70},
    ]
    try:
        result = read_pages(entries, "url", Robots())
    except Exception as e:  # browser binaries not installed
        pytest.skip(f"browser not available: {e}")

    schema = json.loads((SKILL / "schema.json").read_text(encoding="utf-8"))["output"]
    jsonschema.validate(result, schema)
    assert [e["item"]["title"] for e in result["open"]] == ["A"]
    assert [e["item"]["title"] for e in result["closed"]] == ["B"]
    assert [e["item"]["title"] for e in result["unknown"]] == ["C"]
    assert "Python, Selenium" in result["open"][0]["page"]["text"]
    assert result["open"][0]["score"] == 90
