"""Extract job postings from WhatsApp chat exports (.txt / .zip).

This file splits an export into messages; job_blocks.py finds the jobs inside each message.
"""

import argparse
import json
import re
import sys
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from job_blocks import Posting, extract_postings, normalize_url  # noqa: F401 (re-exported)

# Invisible direction marks WhatsApp sprinkles around names and text, plus the
# Hangul filler "ㅤ" some groups use as an empty line.
_INVISIBLE = re.compile("[‎‏‪-‮⁦-⁩ㅤ]")

# "28.10.2026, 14:03 - <rest>"
_MESSAGE_START = re.compile(r"^(\d{1,2}\.\d{1,2}\.\d{4}), (\d{1,2}:\d{2}) - (.*)$")

# Sender is a phone number or "~ name", followed by ": ". System messages have no sender.
_SENDER = re.compile(r"^(\+\d[\d\- ]+|~ ?[^:]{1,40}): (.*)$", re.DOTALL)


@dataclass
class Message:
    group: str
    sent_at: datetime
    sender: str
    text: str


def read_export(path: Path) -> tuple[str, str]:
    """Return (group name, chat text) for a .txt export or a .zip containing one."""
    if path.suffix == ".zip":
        with zipfile.ZipFile(path) as z:
            name = next(n for n in z.namelist() if n.endswith(".txt"))
            text = z.read(name).decode("utf-8-sig")
    else:
        name = path.name
        text = path.read_text(encoding="utf-8-sig")
    group = _INVISIBLE.sub("", Path(name).stem).replace("צ'אט WhatsApp עם", "").strip(" _")
    return group, text


def parse_messages(group: str, text: str) -> list[Message]:
    messages: list[Message] = []
    for line in text.splitlines():
        line = _INVISIBLE.sub("", line)
        start = _MESSAGE_START.match(line)
        if start:
            date, time, rest = start.groups()
            sender = _SENDER.match(rest)
            if not sender:          # system message ("X joined the group" ...)
                continue
            sent_at = datetime.strptime(f"{date} {time}", "%d.%m.%Y %H:%M")
            messages.append(Message(group, sent_at, sender.group(1).strip(), sender.group(2)))
        elif messages:              # continuation line of a multi-line message
            messages[-1].text += "\n" + line
    return messages


def load_postings(exports_dir: Path, since: datetime | None = None) -> list[Posting]:
    """Postings from every export in the folder, de-duplicated by key (earliest kept)."""
    postings: dict[str, Posting] = {}
    for path in sorted(exports_dir.rglob("*")):
        if not path.is_file() or path.suffix not in (".txt", ".zip"):
            continue
        group, text = read_export(path)
        for message in parse_messages(group, text):
            for posting in extract_postings(message.text, message.group, message.sent_at):
                known = postings.get(posting.key)
                if known is None or posting.sent_at < known.sent_at:
                    postings[posting.key] = posting
    return [p for p in postings.values() if since is None or p.sent_at >= since]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("exports_dir", type=Path)
    parser.add_argument("--since", type=datetime.fromisoformat)
    args = parser.parse_args()
    if not args.exports_dir.is_dir():
        parser.error(f"not a folder: {args.exports_dir}")

    rows = [{**asdict(p), "sent_at": p.sent_at.isoformat()}
            for p in load_postings(args.exports_dir, args.since)]
    sys.stdout.reconfigure(encoding="utf-8")
    json.dump(rows, sys.stdout, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
