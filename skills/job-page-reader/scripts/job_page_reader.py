"""Open job posting links in a headless browser, read their text and tell open jobs from closed ones."""

import argparse
import json
import sys
import time
from typing import Callable
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

MAX_TEXT_CHARS = 8000
MIN_TEXT_CHARS = 200        # less than this is a blank or blocked page, not a job description
PAGE_TIMEOUT_MS = 30_000
SETTLE_MS = 15_000          # most extra time for pages that load the job with JavaScript
STABLE_MS = 2_000           # text unchanged this long means the page is done
POLL_MS = 1_000
PAUSE_SECONDS = 1.0         # between pages, to be polite to the sites
USER_AGENT_NAME = "job-page-reader"
SIGN_IN_REASON = "site asks to sign in"

CLOSED_PHRASES = [
    "no longer available",
    "no longer accepting",
    "no longer open",
    "position is missing",
    "not available anymore",
    "job has expired",
    "job is closed",
    "position has been filled",
    "position is closed",
    "המשרה הזו כבר לא פתוחה",
    "המשרה כבר לא פתוחה",
    "המשרה אינה פעילה",
    "המשרה סגורה",
    "המשרה כבר לא זמינה",
    "המשרה אוישה",
]


# A site showing one of these wants a person, not a program. The page is not read, and the rest of
# that site's pages are skipped for the run.
SIGN_IN_WALL_PHRASES = [
    "sign in to apply",
    "log in to apply",
    "sign in to continue",
    "log in to continue",
    "verify you are human",
    "are you a robot",
    "התחבר כדי להגיש",
    "התחברו כדי להגיש",
    "התחבר כדי להמשיך",
]


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def classify(url: str, final_url: str | None, http_status: int | None, text: str) -> tuple[str, str]:
    """Return (status, reason), where status is "open", "closed" or "unknown"."""
    phrase = closed_phrase(text)
    if phrase:
        return "closed", f'page says "{phrase}"'
    if sign_in_wall(text):
        return "unknown", SIGN_IN_REASON
    if http_status in (404, 410):
        return "closed", f"HTTP {http_status}"
    if final_url and _redirected_to_parent(url, final_url):
        return "closed", "redirected to a general page"
    if http_status and http_status >= 400:
        return "unknown", f"HTTP {http_status}"
    if len(text.strip()) < MIN_TEXT_CHARS:
        return "unknown", "page has almost no text"
    return "open", "job text found"


def closed_phrase(text: str) -> str | None:
    lowered = text.lower()
    return next((phrase for phrase in CLOSED_PHRASES if phrase in lowered), None)


def sign_in_wall(text: str) -> bool:
    lowered = text.lower()
    return any(phrase in lowered for phrase in SIGN_IN_WALL_PHRASES)


def _redirected_to_parent(url: str, final_url: str) -> bool:
    """A job link that lands on its company's job list usually means the job was removed."""
    before, after = urlsplit(url), urlsplit(final_url)
    old, new = before.path.rstrip("/"), after.path.rstrip("/")
    return before.netloc == after.netloc and old != new and old.startswith(new + "/")


def settled_text(read: Callable[[], str], wait: Callable[[int], None]) -> str:
    """Pages that build the job with JavaScript show the site menu first and the job later, and
    network activity is not a reliable sign that they are done. So read until the text stops growing."""
    text = read()
    waited = stable_for = 0
    while waited < SETTLE_MS:
        wait(POLL_MS)
        waited += POLL_MS
        latest = read()
        stable_for = stable_for + POLL_MS if len(latest) == len(text) else 0
        text = latest
        if (len(text) >= MIN_TEXT_CHARS and stable_for >= STABLE_MS) or closed_phrase(text):
            break
    return text


def site_of(url: str) -> str:
    """juniors.example.com and www.example.com are the same site."""
    labels = urlsplit(url).netloc.lower().split(".")
    # example.co.il: the site is three labels long
    keep = 3 if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in ("co", "org", "ac", "gov", "net") else 2
    return ".".join(labels[-keep:])


def entries_from(data) -> list[dict]:
    """Accept a JSON array, or the object written by rule-filter (kept) or profile-matcher (scored)."""
    if isinstance(data, dict):
        for key in ("scored", "kept"):
            if key in data:
                return data[key]
        raise ValueError('expected a JSON array or an object with "scored" or "kept"')
    return data


def url_of(entry: dict, field: str) -> str | None:
    return entry.get(field) or (entry.get("item") or {}).get(field)


class Robots:
    """Caches robots.txt per site. A site whose robots.txt cannot be read is treated as allowed."""

    def __init__(self):
        self._parsers: dict[str, RobotFileParser | None] = {}

    def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            return True
        site = f"{parts.scheme}://{parts.netloc}"
        if site not in self._parsers:
            parser = RobotFileParser(site + "/robots.txt")
            try:
                parser.read()
            except Exception:
                parser = None
            self._parsers[site] = parser
        parser = self._parsers[site]
        return parser is None or parser.can_fetch(USER_AGENT_NAME, url)


def read_pages(entries: list[dict], url_field: str, robots: Robots) -> dict:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    result = {"open": [], "closed": [], "unknown": []}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        # Only the text matters, so skip heavy downloads.
        context.route("**/*", lambda route: route.abort()
                      if route.request.resource_type in ("image", "media", "font") else route.continue_())
        walled_sites = set()
        for n, entry in enumerate(entries, 1):
            url = url_of(entry, url_field)
            page_info = {"final_url": None, "text": ""}
            if not url:
                status, reason = "unknown", f'no "{url_field}" field'
            elif site_of(url) in walled_sites:
                status, reason = "unknown", f"{SIGN_IN_REASON} (earlier in this run)"
            elif not robots.allowed(url):
                status, reason = "unknown", "robots.txt does not allow reading this page"
            else:
                page = context.new_page()
                try:
                    response = page.goto(url, timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
                    text = settled_text(lambda: page.inner_text("body"), page.wait_for_timeout)
                    page_info = {"final_url": page.url, "text": text[:MAX_TEXT_CHARS]}
                    status, reason = classify(url, page.url, response.status if response else None, text)
                    if reason == SIGN_IN_REASON:
                        walled_sites.add(site_of(url))
                        page_info["text"] = ""  # the wall, not the job
                except PlaywrightError as e:
                    status, reason = "unknown", f"could not load page ({type(e).__name__})"
                finally:
                    page.close()
                time.sleep(PAUSE_SECONDS)
            result[status].append({**entry, "page": {"status": status, "reason": reason, **page_info}})
            log(f"{n}/{len(entries)} {status}: {url}")
        browser.close()
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=15, help="read only the first N entries (default 15)")
    parser.add_argument("--url-field", default="url")
    args = parser.parse_args()

    entries = entries_from(json.loads(sys.stdin.buffer.read().decode("utf-8-sig")))[:args.limit]
    result = read_pages(entries, args.url_field, Robots())
    sys.stdout.reconfigure(encoding="utf-8")
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
