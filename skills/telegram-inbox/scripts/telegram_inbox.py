"""Collect job posts shared by hand to a Telegram bot, from the inbox Worker, as structured postings.

  pull <inbox-dir> [--url URL]      fetch waiting posts and feedback, save them to inbox.jsonl and
                                    events.jsonl, then delete them from the Worker.
                                    Prints {"pulled": n, "saved": m, "events": k}.
  read <inbox-dir> [--since TIME]   job postings from inbox.jsonl, in the whatsapp-export-reader format.
  events <inbox-dir>                saved feedback events (button presses and replies), oldest first.
  exports <exports-dir> [--url URL] download the chat export files shared to the bot into the folder.
                                    Prints {"downloaded": [file names]}.

The Worker URL comes from --url or INBOX_URL, and its token from INBOX_TOKEN.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

# The job parsing is shared with whatsapp-export-reader, which lives next to this skill.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "whatsapp-export-reader" / "scripts"))
from job_blocks import Posting, extract_postings, split_copied_messages  # noqa: E402

INBOX_FILE = "inbox.jsonl"
EVENTS_FILE = "events.jsonl"
GROUP = "telegram-inbox"
TIMEOUT_SECONDS = 20
ATTEMPTS = 3          # some networks drop connections now and then
RETRY_SECONDS = 1


class InboxError(Exception):
    pass


def pull(inbox_dir: Path, url: str, token: str) -> dict:
    """Save waiting posts and feedback first, delete them from the Worker second.

    A crash in between leaves them on the Worker, so the next pull fetches them again;
    they are recognized by update_id and not saved twice. Nothing is lost either way.
    """
    waiting = _call(url, "GET", "/inbox", token)
    messages, events = waiting["messages"], waiting.get("events", [])
    if not messages and not events:
        return {"pulled": 0, "saved": 0, "events": 0}

    inbox_dir.mkdir(parents=True, exist_ok=True)
    saved = _append_new(inbox_dir / INBOX_FILE, [_to_record(m) for m in messages])
    saved_events = _append_new(inbox_dir / EVENTS_FILE, events)

    _call(url, "POST", "/inbox/ack", token, {"update_ids": [r["update_id"] for r in messages + events]})
    return {"pulled": len(messages), "saved": saved, "events": saved_events}


def _append_new(path: Path, records: list[dict]) -> int:
    known = {record["update_id"] for record in _records(path)}
    new = [r for r in records if r["update_id"] not in known]
    if new:
        with path.open("a", encoding="utf-8") as f:
            for record in new:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
    return len(new)


def events(inbox_dir: Path) -> list[dict]:
    """Feedback on digest jobs, in the order it was given."""
    return sorted(_records(inbox_dir / EVENTS_FILE), key=lambda e: e["update_id"])


def download_exports(exports_dir: Path, url: str, token: str) -> list[str]:
    """Chat exports shared to the bot. They stay on the Worker, like exports kept on disk, until a
    newer export of the same group replaces them or they expire."""
    exports_dir.mkdir(parents=True, exist_ok=True)
    names = []
    for entry in _call(url, "GET", "/exports", token)["exports"]:
        name = Path(entry["name"]).name     # the Worker already strips folders; never trust it twice
        if not name or name.startswith("."):
            continue
        data = _call(url, "GET", "/exports/file?name=" + urllib.parse.quote(entry["name"]), token, raw=True)
        (exports_dir / name).write_bytes(data)
        names.append(name)
    return names


def _to_record(message: dict) -> dict:
    """One stored line per Telegram message. Copied WhatsApp headers, with sender names and phone
    numbers, are removed here, before anything touches the disk."""
    received_at = datetime.fromtimestamp(message["date"])
    parts = split_copied_messages(message["text"], received_at)
    return {
        "update_id": message["update_id"],
        "received_at": received_at.isoformat(),
        "messages": [{"sent_at": sent_at.isoformat(), "text": text} for sent_at, text in parts],
    }


def read(inbox_dir: Path, since: datetime | None = None) -> list[Posting]:
    """Postings from every saved message, de-duplicated by key (earliest kept)."""
    postings: dict[str, Posting] = {}
    for record in _records(inbox_dir / INBOX_FILE):
        for message in record["messages"]:
            for posting in extract_postings(message["text"], GROUP, datetime.fromisoformat(message["sent_at"])):
                known = postings.get(posting.key)
                if known is None or posting.sent_at < known.sent_at:
                    postings[posting.key] = posting
    return [p for p in postings.values() if since is None or p.sent_at >= since]


def _records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _call(url: str, method: str, route: str, token: str, body: dict | None = None, raw: bool = False):
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url.rstrip("/") + route, data=data, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "telegram-inbox-skill",
    })
    # Both routes are safe to repeat: fetching changes nothing, and deleting twice deletes nothing more.
    for attempt in range(1, ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                content = response.read()
                return content if raw else json.loads(content.decode("utf-8"))
        except urllib.error.HTTPError as e:
            hint = " (check INBOX_TOKEN)" if e.code == 401 else ""
            raise InboxError(f"{method} {route} failed: HTTP {e.code}{hint}") from None
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            if attempt == ATTEMPTS:
                raise InboxError(f"{method} {route} failed: {getattr(e, 'reason', e)}") from None
            time.sleep(RETRY_SECONDS * attempt)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    pull_cmd = commands.add_parser("pull", help="fetch waiting posts and feedback from the Worker")
    pull_cmd.add_argument("inbox_dir", type=Path)
    pull_cmd.add_argument("--url", default=os.environ.get("INBOX_URL"))
    read_cmd = commands.add_parser("read", help="job postings from saved messages")
    read_cmd.add_argument("inbox_dir", type=Path)
    read_cmd.add_argument("--since", type=datetime.fromisoformat)
    events_cmd = commands.add_parser("events", help="saved feedback on digest jobs")
    events_cmd.add_argument("inbox_dir", type=Path)
    exports_cmd = commands.add_parser("exports", help="download chat exports shared to the bot")
    exports_cmd.add_argument("exports_dir", type=Path)
    exports_cmd.add_argument("--url", default=os.environ.get("INBOX_URL"))
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    if args.command in ("pull", "exports"):
        token = os.environ.get("INBOX_TOKEN")
        if not args.url or not token:
            parser.error("set INBOX_URL (or --url) and INBOX_TOKEN")
        try:
            if args.command == "pull":
                result = pull(args.inbox_dir, args.url, token)
            else:
                result = {"downloaded": download_exports(args.exports_dir, args.url, token)}
        except InboxError as e:
            sys.exit(f"telegram-inbox: {e}")
        json.dump(result, sys.stdout, ensure_ascii=False)
    elif args.command == "events":
        json.dump(events(args.inbox_dir), sys.stdout, ensure_ascii=False, indent=2)
    else:
        rows = [{**asdict(p), "sent_at": p.sent_at.isoformat()} for p in read(args.inbox_dir, args.since)]
        json.dump(rows, sys.stdout, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
