"""Extract job postings from WhatsApp chat exports (.txt / .zip).

Every job posting in the groups ends with a link, so a message is split into job
"blocks": the text between one job link and the next.
"""

import argparse
import json
import re
import sys
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Invisible direction marks WhatsApp sprinkles around names and text, plus the
# Hangul filler "ㅤ" some groups use as an empty line.
_INVISIBLE = re.compile("[‎‏‪-‮⁦-⁩ㅤ]")

# "28.10.2026, 14:03 - <rest>"
_MESSAGE_START = re.compile(r"^(\d{1,2}\.\d{1,2}\.\d{4}), (\d{1,2}:\d{2}) - (.*)$")

# Sender is a phone number or "~ name", followed by ": ". System messages have no sender.
_SENDER = re.compile(r"^(\+\d[\d\- ]+|~ ?[^:]{1,40}): (.*)$", re.DOTALL)

_URL = re.compile(r"https?://[^\s<>\"*]+")

# Links that appear in job messages but are not jobs (ads, group invites, app stores...).
_NOISE_URL = re.compile(
    r"""
    hiremetech\.com/(?!job/)        # hiremetech: only /job/ links are jobs
  | chat\.hiremetech\.com
  | secrethunter\.io/(?!jobz/)      # secrethunter: only /jobz/ links are jobs
  | hire\.secrethunter\.io
  | play\.google\.com | apps\.apple\.com
  | linkedin\.com/(feed|posts|in)/
  | chat\.whatsapp\.com
  | airtable\.com
    """,
    re.VERBOSE | re.IGNORECASE,
)

# Query parameters that only track where a click came from. Others can identify the job (?jid=123).
_TRACKING_PARAM = re.compile(r"^(utm_.*|ref|src|source|fbclid|gclid|gh_src|trk)$", re.IGNORECASE)

_TITLE_LABEL = re.compile(r"^\s*\*?שם המשרה:?\*?:?\s*(.+)$")
_COMPANY_LABEL = re.compile(r"^\s*\*?שם החברה:?\*?:?\s*(.*)$")
_BOLD = re.compile(r"\*([^*\n]{3,150})\*")


@dataclass
class Message:
    group: str
    sent_at: datetime
    sender: str
    text: str


@dataclass
class Posting:
    key: str             # normalized URL, the identity used for de-duplication
    url: str
    title: str
    company: str
    text: str            # the raw block of text describing this job
    group: str
    sent_at: datetime


def normalize_url(url: str) -> str:
    """Canonical form of a job link, so the same job matches across groups and runs."""
    parts = urlsplit(url.rstrip(".,)"))
    host = parts.netloc.lower().removeprefix("www.").removeprefix("juniors.")
    query = urlencode(sorted((k, v) for k, v in parse_qsl(parts.query) if not _TRACKING_PARAM.match(k)))
    return urlunsplit(("https", host, parts.path.rstrip("/"), query, ""))


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


def extract_postings(message: Message) -> list[Posting]:
    postings: list[Posting] = []
    block_start = 0
    for match in _URL.finditer(message.text):
        url = match.group().rstrip(".,)")
        if _NOISE_URL.search(url):
            continue
        block = message.text[block_start:match.start()]
        block_start = match.end()
        title, company = _guess_title_company(block, url)
        if title:
            postings.append(Posting(normalize_url(url), url, title, company, _clean(block),
                                    message.group, message.sent_at))
    return postings


def _guess_title_company(block: str, url: str) -> tuple[str, str]:
    lines = [l.strip() for l in block.splitlines() if l.strip()]
    title = company = ""
    for line in lines:
        if m := _TITLE_LABEL.match(line):
            title = m.group(1)
        elif m := _COMPANY_LABEL.match(line):
            company = m.group(1)
    if not title:
        bold = _BOLD.findall(block)
        if bold:
            title = bold[0]
            # Secret Hunter digest style: "*Title* - Company"
            after = block.split(f"*{title}*", 1)[1].splitlines()[0]
            if after.strip().startswith("-"):
                company = after.strip(" -")
            # Secret Hunter post style: company on the line after the title
            elif "secrethunter" in url and len(lines) > 1 and lines[0].startswith("*"):
                company = lines[1]
    return title.strip(" *_-"), company.strip(" *_-")


def _clean(block: str) -> str:
    lines = [l.strip() for l in block.splitlines()]
    return "\n".join(l for l in lines if l and l != "--")


def load_postings(exports_dir: Path, since: datetime | None = None) -> list[Posting]:
    """Postings from every export in the folder, de-duplicated by key (earliest kept)."""
    postings: dict[str, Posting] = {}
    for path in sorted(exports_dir.rglob("*")):
        if not path.is_file() or path.suffix not in (".txt", ".zip"):
            continue
        group, text = read_export(path)
        for message in parse_messages(group, text):
            for posting in extract_postings(message):
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
