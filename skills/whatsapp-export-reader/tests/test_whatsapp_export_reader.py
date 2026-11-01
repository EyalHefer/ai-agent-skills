import json
import subprocess
import sys
import zipfile
from datetime import datetime
from pathlib import Path

import jsonschema

SKILL = Path(__file__).resolve().parent.parent
SCRIPT = SKILL / "scripts" / "whatsapp_export_reader.py"
sys.path.insert(0, str(SCRIPT.parent))

from whatsapp_export_reader import load_postings, normalize_url  # noqa: E402

# All names, numbers and links below are invented.
EXPORT = (
    "1.10.2026, 9:00 - ‏ההודעות והשיחות מוצפנות מקצה לקצה\n"
    "1.10.2026, 9:01 - ‏‪+972 50-000-0001‬‏ הצטרף/ה לקבוצה באמצעות קישור.\n"
    "2.10.2026, 10:00 - ‏‪+972 50-000-0002‬‏: בוקר טוב!\n"
    "\n"
    "שם המשרה: Junior Backend Developer\n"
    "שם החברה: Acme\n"
    "לינק להגשה: https://acme.example/jobs/1?utm_source=wa\n"
    "\n"
    "*שם המשרה:* QA Automation Engineer\n"
    "*שם החברה:* Globex\n"
    "*לינק להגשה:* https://globex.example/careers/?jid=20\n"
    "3.10.2026, 11:00 - ‏‪+972 50-000-0003‬‏: *Software Development Student*\n"
    "Level: Student\n"
    "https://juniors.hiremetech.com/job/111?utm_source=wa_a\n"
    "https://hiremetech.com/communities?utm_source=wa_a\n"
    "4.10.2026, 12:00 - ‏~ Recruiter‏: *Software Development Student*\n"
    "https://hiremetech.com/job/111?utm_source=share\n"
    "5.10.2026, 13:00 - ‏‪+972 50-000-0004‬‏: *Student software engineer*\n"
    "Initech\n"
    "Tel Aviv\n"
    "https://secrethunter.io/jobz/abc?utm_source=whatsapp\n"
    "--\n"
    "Browse companies:\n"
    "https://secrethunter.io/companies?utm_source=whatsapp\n"
    "6.10.2026, 14:00 - ‏‪+972 50-000-0005‬‏: מישהו יודע אם יש חניה?\n"
)


def write_export(folder: Path) -> Path:
    path = folder / "group.txt"
    path.write_text(EXPORT, encoding="utf-8")
    return path


def by_title(postings):
    return {p.title: p for p in postings}


def test_one_message_with_several_labelled_jobs(tmp_path):
    write_export(tmp_path)
    postings = by_title(load_postings(tmp_path))
    assert postings["Junior Backend Developer"].company == "Acme"
    assert postings["QA Automation Engineer"].company == "Globex"


def test_bold_title_and_company_on_next_line(tmp_path):
    write_export(tmp_path)
    assert by_title(load_postings(tmp_path))["Student software engineer"].company == "Initech"


def test_level_line_is_not_taken_as_company(tmp_path):
    write_export(tmp_path)
    assert by_title(load_postings(tmp_path))["Software Development Student"].company == ""


def test_noise_links_and_chat_are_ignored(tmp_path):
    write_export(tmp_path)
    urls = [p.url for p in load_postings(tmp_path)]
    assert len(urls) == 4
    assert not any("communities" in u or "companies" in u for u in urls)


def test_duplicate_job_keeps_earliest_posting(tmp_path):
    write_export(tmp_path)
    posting = by_title(load_postings(tmp_path))["Software Development Student"]
    assert posting.sent_at == datetime(2026, 10, 3, 11, 0)


def test_same_job_in_txt_and_zip_is_merged(tmp_path):
    export = write_export(tmp_path)
    with zipfile.ZipFile(tmp_path / "group.zip", "w") as z:
        z.write(export, "group.txt")
    assert len(load_postings(tmp_path)) == 4


def test_since_filters_by_send_time(tmp_path):
    write_export(tmp_path)
    assert len(load_postings(tmp_path, since=datetime(2026, 10, 5))) == 1


def test_normalize_drops_tracking_but_keeps_job_id():
    assert normalize_url("https://www.a.example/x/?utm_source=wa") == "https://a.example/x"
    assert normalize_url("https://a.example/job/?jid=1") != normalize_url("https://a.example/job/?jid=2")


def test_cli_output_matches_schema(tmp_path):
    write_export(tmp_path)
    result = subprocess.run([sys.executable, str(SCRIPT), str(tmp_path)],
                            capture_output=True, encoding="utf-8", check=True)
    schema = json.loads((SKILL / "schema.json").read_text(encoding="utf-8"))
    rows = json.loads(result.stdout)
    assert len(rows) == 4
    jsonschema.validate(rows, schema["output"])
