---
name: job-page-reader
description: Open job posting links in a headless browser, read the full job text and sort the jobs into open, closed and unknown. Use after ranking, on the top jobs only, to drop closed jobs and to give the next scoring step the real description instead of just a title.
---

# Job Page Reader

Many job sites build the page with JavaScript, so a plain HTTP request gets the same empty page for every job. This skill uses a real browser (Chromium, headless) for a short list of links.

## Run

```
python scripts/job_page_reader.py [--limit 15] [--url-field url] < entries.json
```

Input is a JSON array, or the object written by `rule-filter` (`kept`) or `profile-matcher` (`scored`). The link is read from the entry itself or from its `item`. Each entry is returned unchanged, with a `page` field added, under `open`, `closed` or `unknown`. Output is defined in [schema.json](schema.json). Progress goes to stderr.

Setup, once: `pip install playwright` and `python -m playwright install chromium`.

## How a job is judged closed

- The page says so, for example "no longer available" or "המשרה הזו כבר לא פתוחה". This is checked first, because some sites still show the old description under the notice.
- HTTP 404 or 410.
- The link redirects to a parent page on the same site, such as the company's job list.

A page that fails to load, is blocked, or has almost no text is `unknown`, never `closed`, so a job is not dropped by mistake.

## Being a good guest

- Pages are read one at a time, with a pause between them, and images and fonts are not downloaded.
- `robots.txt` is respected. A page it disallows is returned as `unknown` without being opened.
- A site that answers with a sign-in or "are you a robot" page wants a person, not a program. That page is `unknown`, and the rest of the site's pages are skipped for the run.
- It reads pages only. It does not log in, fill forms or apply.
- Keep `--limit` small. The skill is meant for a short list of top jobs, not for crawling a site.
