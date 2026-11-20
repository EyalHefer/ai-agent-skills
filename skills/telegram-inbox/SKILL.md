---
name: telegram-inbox
description: Collect what a person sent a private Telegram bot (job posts shared by hand, WhatsApp chat export files, feedback button presses and replies) from an inbox Cloudflare Worker. Posts come back as structured postings in the whatsapp-export-reader format. Use when a source cannot be exported and the person forwards posts manually, or to take exports and feedback from the phone.
---

# Telegram Inbox

Some WhatsApp groups turn on Advanced Chat Privacy, which blocks exporting the chat. This skill does
not read WhatsApp. A person selects job posts on their phone and shares them to a private Telegram
bot. A small Worker ([job-search-agent/worker](https://github.com/EyalHefer/job-search-agent/tree/main/worker))
keeps them until this skill collects them.

## Run

```
python scripts/telegram_inbox.py pull <inbox-dir> [--url https://...workers.dev]
python scripts/telegram_inbox.py read <inbox-dir> [--since 2026-11-18T00:00]
python scripts/telegram_inbox.py events <inbox-dir>
python scripts/telegram_inbox.py exports <exports-dir> [--url https://...workers.dev]
```

- `pull` and `exports` need `INBOX_TOKEN` in the environment, and the Worker URL in `--url` or `INBOX_URL`.
- `pull` saves posts to `inbox.jsonl` and feedback to `events.jsonl`, and prints
  `{"pulled": n, "saved": m, "events": k}`.
- `read` works offline and writes a JSON array in the same format as whatsapp-export-reader,
  defined in [schema.json](schema.json), with `group` set to `telegram-inbox`.
- `events` works offline and prints the saved feedback events, oldest first: a button press
  `{"update_id", "type": "button", "message_id", "choice", "date"}` or a reply
  `{"update_id", "type": "reply", "message_id", "text", "date"}`. `message_id` is the bot message it
  was about; mapping it to a job is the caller's part.
- `exports` downloads the chat export files shared to the bot into the folder, ready for
  whatsapp-export-reader, and prints `{"downloaded": [file names]}`. They stay on the Worker.

## How it works

1. `pull` fetches the waiting messages, appends them to `inbox.jsonl`, and only then asks the Worker
   to delete them. A crash in between repeats a message on the next pull (skipped by `update_id`),
   but never loses one.
2. Before saving, text copied from several WhatsApp messages at once is split on the
   `[18.11, 12:29] sender:` headers the phone adds. The sender line, with its name or phone number,
   is dropped, so it is never stored or sent to an LLM.
3. `read` finds the jobs in each message with `job_blocks.py`, shared with whatsapp-export-reader
   (this skill needs that folder next to it), and removes duplicates by `key`.

## Limitations

- The copied header carries no year; it is taken from when the bot received the message.
- Jobs without a link (for example behind a paywall) are skipped, as in whatsapp-export-reader.
- `inbox.jsonl` holds job post text. Keep it out of public repos.
- Downloaded exports hold other people's messages and numbers. Keep them out of git entirely.
