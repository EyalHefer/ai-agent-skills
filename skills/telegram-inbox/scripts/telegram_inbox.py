"""Collect job posts shared by hand to a Telegram bot, from the inbox Worker, as structured postings.

  pull <inbox-dir> [--url URL]      fetch waiting messages, save them to inbox.jsonl, then delete them
                                    from the Worker. Prints {"pulled": n, "saved": m}.
  read <inbox-dir> [--since TIME]   job postings from inbox.jsonl, in the whatsapp-export-reader format.

The Worker URL comes from --url or INBOX_URL, and its token from INBOX_TOKEN.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

# The job parsing is shared with whatsapp-export-reader, which lives next to this skill.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "whatsapp-export-reader" / "scripts"))
from job_blocks import Posting, extract_postings, split_copied_messages  # noqa: E402

INBOX_FILE = "inbox.jsonl"
GROUP = "telegram-inbox"
TIMEOUT_SECONDS = 20
ATTEMPTS = 3          # some networks drop connections now and then
RETRY_SECONDS = 1


class InboxError(Exception):
    pass


def pull(inbox_dir: Path, url: str, token: str) -> dict:
    """Save waiting messages first, delete them from the Worker second.

    A crash in between leaves them on the Worker, so the next pull fetches them again;
    they are recognized by update_id and not saved twice. Nothing is lost either way.
    """
    waiting = _call(url, "GET", "/inbox", token)["messages"]
    if not waiting:
        return {"pulled": 0, "saved": 0}

    inbox_dir.mkdir(parents=True, exist_ok=True)
    path = inbox_dir / INBOX_FILE
    known = {record["update_id"] for record in _records(path)}
    new = [m for m in waiting if m["update_id"] not in known]
    with path.open("a", encoding="utf-8") as f:
        for message in new:
            f.write(json.dumps(_to_record(message), ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())

    _call(url, "POST", "/inbox/ack", token, {"update_ids": [m["update_id"] for m in waiting]})
    return {"pulled": len(waiting), "saved": len(new)}


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


def _call(url: str, method: str, route: str, token: str, body: dict | None = None) -> dict:
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
                return json.loads(response.read().decode("utf-8"))
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
    pull_cmd = commands.add_parser("pull", help="fetch waiting messages from the Worker")
    pull_cmd.add_argument("inbox_dir", type=Path)
    pull_cmd.add_argument("--url", default=os.environ.get("INBOX_URL"))
    read_cmd = commands.add_parser("read", help="job postings from saved messages")
    read_cmd.add_argument("inbox_dir", type=Path)
    read_cmd.add_argument("--since", type=datetime.fromisoformat)
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    if args.command == "pull":
        token = os.environ.get("INBOX_TOKEN")
        if not args.url or not token:
            parser.error("set INBOX_URL (or --url) and INBOX_TOKEN")
        try:
            result = pull(args.inbox_dir, args.url, token)
        except InboxError as e:
            sys.exit(f"telegram-inbox: {e}")
        json.dump(result, sys.stdout)
    else:
        rows = [{**asdict(p), "sent_at": p.sent_at.isoformat()} for p in read(args.inbox_dir, args.since)]
        json.dump(rows, sys.stdout, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
