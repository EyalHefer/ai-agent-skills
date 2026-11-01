---
name: whatsapp-export-reader
description: Extract structured job postings from WhatsApp group chat exports (.txt or .zip). Use when an agent needs the jobs posted in WhatsApp groups as data. Reads local files only.
---

# WhatsApp Export Reader

Reads the files WhatsApp creates with **Export chat → Without media** and returns one record per job posting.

## Run

```
python scripts/whatsapp_export_reader.py <exports-dir> [--since 2026-10-28T00:00]
```

Writes a JSON array to stdout. The format is defined in [schema.json](schema.json).

## How it works

1. Splits the export into messages and skips system messages such as "X joined the group".
2. Splits each message into job blocks. Each job ends at its link, so one message can hold several jobs.
3. Ignores links that are not jobs: ads, group invites and app stores (`_NOISE_URL` in the script).
4. Removes duplicates across groups by `key`, keeping the earliest posting.

## Limitations

- Expects the `28.10.2026, 14:03 - ` date format. Exports from phones with another date format are not parsed.
- Recognizes titles from Hebrew labels (`שם המשרה:`) or from the first *bold* text. A job with neither is skipped.
- `company` is empty when the message does not name one.
- WhatsApp messages contain phone numbers. Never commit real exports.
