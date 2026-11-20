import json
import subprocess
import sys
import threading
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import jsonschema
import pytest

SKILL = Path(__file__).resolve().parent.parent
SCRIPT = SKILL / "scripts" / "telegram_inbox.py"
sys.path.insert(0, str(SCRIPT.parent))

import telegram_inbox  # noqa: E402
from telegram_inbox import InboxError, download_exports, events, pull, read  # noqa: E402

TOKEN = "test-token"
NOV_18_15H = int(datetime(2026, 11, 18, 15, 0).timestamp())

# Everything below is invented: names, numbers, companies and links.
ONE_JOB = "*Junior QA Engineer* / Acme\n\n_Tel Aviv_ | _QA_\n\nhttps://acme.example/jobs/1?utm_source=tg\n"
COPIED = (
    "[18.11, 12:30] +1 (555) 010-0000: *Data Analyst* / Globex\n\nhttps://globex.example/jobs/2\n"
    "[18.11, 14:03] @JobsBot: *Backend Developer* / Initech\n\nhttps://initech.example/jobs/3\n"
    "*Book a prep session with Dana* ✅\nhttps://coach.setmore.com/dana\n"
)


class FakeWorker:
    """The inbox Worker's agent routes, served on localhost."""

    def __init__(self, messages, fail_ack=False, drops=0, events=(), exports=None):
        self.messages = list(messages)
        self.events = list(events)
        self.exports = dict(exports or {})
        self.fail_ack = fail_ack
        self.drops = drops          # connections to close without an answer, as a flaky network does
        worker = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status, body):
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body if isinstance(body, bytes) else json.dumps(body).encode())

            def _authorized(self):
                return self.headers.get("Authorization") == f"Bearer {TOKEN}"

            def do_GET(self):
                if worker.drops:
                    worker.drops -= 1
                    self.close_connection = True
                    return
                if not self._authorized():
                    return self._send(401, {})
                path = urllib.parse.urlsplit(self.path)
                if path.path == "/exports":
                    return self._send(200, {"exports": [{"name": n, "size": len(b)} for n, b in worker.exports.items()]})
                if path.path == "/exports/file":
                    name = urllib.parse.parse_qs(path.query)["name"][0]
                    return self._send(200, worker.exports[name]) if name in worker.exports else self._send(404, {})
                body = {"messages": worker.messages}
                if worker.events is not None:       # None plays a Worker from before feedback existed
                    body["events"] = worker.events
                self._send(200, body)

            def do_POST(self):
                if not self._authorized():
                    return self._send(401, {})
                if worker.fail_ack:
                    return self._send(500, {})
                ids = json.loads(self.rfile.read(int(self.headers["Content-Length"])))["update_ids"]
                worker.messages = [m for m in worker.messages if m["update_id"] not in ids]
                worker.events = [e for e in worker.events or [] if e["update_id"] not in ids]
                self._send(200, {"deleted": len(ids)})

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@pytest.fixture(autouse=True)
def no_retry_wait(monkeypatch):
    monkeypatch.setattr(telegram_inbox, "RETRY_SECONDS", 0)


@pytest.fixture
def worker():
    servers = []

    def start(messages, **kwargs):
        servers.append(FakeWorker(messages, **kwargs))
        return servers[-1]

    yield start
    for s in servers:
        s.close()


def message(update_id, text):
    return {"update_id": update_id, "date": NOV_18_15H, "text": text}


def test_pull_saves_messages_then_deletes_them_from_the_worker(tmp_path, worker):
    w = worker([message(1, ONE_JOB), message(2, COPIED)])
    assert pull(tmp_path, w.url, TOKEN) == {"pulled": 2, "saved": 2, "events": 0}
    assert w.messages == []
    assert len((tmp_path / "inbox.jsonl").read_text(encoding="utf-8").splitlines()) == 2


def test_sender_names_and_numbers_never_reach_the_disk(tmp_path, worker):
    pull(tmp_path, worker([message(1, COPIED)]).url, TOKEN)
    saved = (tmp_path / "inbox.jsonl").read_text(encoding="utf-8")
    assert "555" not in saved and "JobsBot" not in saved


def test_failed_ack_keeps_messages_and_the_retry_does_not_duplicate(tmp_path, worker):
    w = worker([message(1, ONE_JOB)], fail_ack=True)
    with pytest.raises(InboxError):
        pull(tmp_path, w.url, TOKEN)
    assert len(w.messages) == 1                         # still on the Worker, nothing lost
    w.fail_ack = False
    assert pull(tmp_path, w.url, TOKEN) == {"pulled": 1, "saved": 0, "events": 0}
    assert len((tmp_path / "inbox.jsonl").read_text(encoding="utf-8").splitlines()) == 1


def test_wrong_token_is_reported(tmp_path, worker):
    with pytest.raises(InboxError, match="401.*INBOX_TOKEN"):
        pull(tmp_path, worker([]).url, "wrong")


def test_dropped_connections_are_retried(tmp_path, worker):
    w = worker([message(1, ONE_JOB)], drops=2)
    assert pull(tmp_path, w.url, TOKEN) == {"pulled": 1, "saved": 1, "events": 0}


def test_unreachable_worker_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(telegram_inbox, "ATTEMPTS", 1)   # Windows takes ~2s to refuse each connection
    with pytest.raises(InboxError):
        pull(tmp_path, "http://127.0.0.1:9", TOKEN)


def test_read_finds_jobs_with_their_send_times(tmp_path, worker):
    pull(tmp_path, worker([message(1, ONE_JOB), message(2, COPIED)]).url, TOKEN)
    postings = {p.title: p for p in read(tmp_path)}
    assert set(postings) == {"Junior QA Engineer", "Data Analyst", "Backend Developer"}
    assert postings["Data Analyst"].company == "Globex"
    assert postings["Data Analyst"].sent_at == datetime(2026, 11, 18, 12, 30)
    assert postings["Junior QA Engineer"].sent_at == datetime(2026, 11, 18, 15, 0)
    assert postings["Junior QA Engineer"].key == "https://acme.example/jobs/1"


def test_same_job_shared_twice_is_one_posting(tmp_path, worker):
    pull(tmp_path, worker([message(1, ONE_JOB), message(2, ONE_JOB)]).url, TOKEN)
    assert len(read(tmp_path)) == 1


def test_read_without_inbox_file_is_empty(tmp_path):
    assert read(tmp_path) == []


def test_since_filters_by_send_time(tmp_path, worker):
    pull(tmp_path, worker([message(2, COPIED)]).url, TOKEN)
    assert [p.title for p in read(tmp_path, since=datetime(2026, 11, 18, 13, 0))] == ["Backend Developer"]


def test_cli_pull_and_read(tmp_path, worker):
    w = worker([message(1, ONE_JOB), message(2, COPIED)])
    env = {"INBOX_URL": w.url, "INBOX_TOKEN": TOKEN, "PYTHONIOENCODING": "utf-8", "SYSTEMROOT": "C:\\Windows"}
    pulled = subprocess.run([sys.executable, str(SCRIPT), "pull", str(tmp_path)], env=env,
                            capture_output=True, encoding="utf-8", check=True)
    assert json.loads(pulled.stdout) == {"pulled": 2, "saved": 2, "events": 0}

    result = subprocess.run([sys.executable, str(SCRIPT), "read", str(tmp_path)],
                            capture_output=True, encoding="utf-8", check=True)
    schema = json.loads((SKILL / "schema.json").read_text(encoding="utf-8"))
    rows = json.loads(result.stdout)
    assert len(rows) == 3
    jsonschema.validate(rows, schema["output"])


def test_cli_pull_without_settings_fails_clearly(tmp_path):
    result = subprocess.run([sys.executable, str(SCRIPT), "pull", str(tmp_path)],
                            env={"SYSTEMROOT": "C:\\Windows"}, capture_output=True, encoding="utf-8")
    assert result.returncode != 0
    assert "INBOX_TOKEN" in result.stderr


def button(update_id, message_id, choice):
    return {"update_id": update_id, "type": "button", "message_id": message_id, "choice": choice, "date": NOV_18_15H}


def test_pull_saves_feedback_events_and_acks_them(tmp_path, worker):
    w = worker([message(1, ONE_JOB)], events=[button(3, 55, "skipped"), button(2, 55, "applied")])
    assert pull(tmp_path, w.url, TOKEN) == {"pulled": 1, "saved": 1, "events": 2}
    assert w.messages == [] and w.events == []
    assert [e["choice"] for e in events(tmp_path)] == ["applied", "skipped"]   # oldest first


def test_feedback_alone_is_pulled_too(tmp_path, worker):
    w = worker([], events=[button(2, 55, "applied")])
    assert pull(tmp_path, w.url, TOKEN) == {"pulled": 0, "saved": 0, "events": 1}


def test_events_pulled_twice_are_saved_once(tmp_path, worker):
    w = worker([], events=[button(2, 55, "applied")], fail_ack=True)
    with pytest.raises(InboxError):
        pull(tmp_path, w.url, TOKEN)
    w.fail_ack = False
    assert pull(tmp_path, w.url, TOKEN)["events"] == 0
    assert len(events(tmp_path)) == 1


def test_older_worker_without_events_still_works(tmp_path, worker):
    w = worker([message(1, ONE_JOB)])
    w.events = None
    assert pull(tmp_path, w.url, TOKEN)["saved"] == 1


def test_download_exports_writes_each_file_under_its_name(tmp_path, worker):
    w = worker([], exports={"צ'אט WhatsApp עם Jobs.zip": b"PK\x03\x04", "../escape.txt": b"x"})
    names = download_exports(tmp_path / "exports", w.url, TOKEN)
    assert sorted(names) == ["escape.txt", "צ'אט WhatsApp עם Jobs.zip"]
    assert (tmp_path / "exports" / "צ'אט WhatsApp עם Jobs.zip").read_bytes() == b"PK\x03\x04"
    assert not (tmp_path / "escape.txt").exists()


def test_cli_exports_and_events(tmp_path, worker):
    w = worker([], events=[button(2, 55, "applied")], exports={"Jobs.txt": b"hello"})
    env = {"INBOX_URL": w.url, "INBOX_TOKEN": TOKEN, "PYTHONIOENCODING": "utf-8", "SYSTEMROOT": "C:\\Windows"}
    run = lambda *a: subprocess.run([sys.executable, str(SCRIPT), *a], env=env, capture_output=True,
                                    encoding="utf-8", check=True).stdout
    assert json.loads(run("exports", str(tmp_path / "ex"))) == {"downloaded": ["Jobs.txt"]}
    run("pull", str(tmp_path / "inbox"))
    assert json.loads(run("events", str(tmp_path / "inbox")))[0]["choice"] == "applied"
