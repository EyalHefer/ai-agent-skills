"""Find job postings in the text of a chat message.

Every job posting ends with a link, so a message is split into job "blocks": the text between one
job link and the next. This module knows nothing about where the text came from (a WhatsApp export,
a message forwarded to a bot...), so any reader of chat text can use it.
"""

import re
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

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


def extract_postings(text: str, group: str, sent_at: datetime) -> list[Posting]:
    """The job postings in one message. Blocks without a recognizable title are skipped."""
    postings: list[Posting] = []
    block_start = 0
    for match in _URL.finditer(text):
        url = match.group().rstrip(".,)")
        if _NOISE_URL.search(url):
            continue
        block = text[block_start:match.start()]
        block_start = match.end()
        title, company = _guess_title_company(block, url)
        if title:
            postings.append(Posting(normalize_url(url), url, title, company, _clean(block), group, sent_at))
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
