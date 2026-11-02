---
name: profile-matcher
description: Score a JSON list of items against a person's profile with an LLM, returning a 0-100 score, a reason and the gaps for each item. Use to rank filtered items such as job postings or GitHub issues. Requires a GEMINI_API_KEY.
---

# Profile Matcher

Sends items to Gemini in large batches and returns them ranked, best first.

## Run

```
python scripts/profile_matcher.py --instructions scoring.md --context profile.md [--context more.md] < items.json
```

- `--instructions` says how to score. This is what makes the skill fit a specific agent.
- `--context` adds what the model should know, such as the profile, preferences and past ratings. Repeatable.
- `--fields` lists the item fields sent to the model (default `title,company,text`).
- `--max-chars` cuts each field to this length (default 1500). Raise it when items carry full page text.

Input is a JSON array, or the object written by `rule-filter`, in which case `kept` is used. Output is defined in [schema.json](schema.json). Progress goes to stderr.

## Reliability

- Models are tried in order. When one is busy, the next is tried at once.
- If every model is busy, it waits and retries, then moves on.
- Items that could not be scored are returned in `unscored` instead of failing the run, so they can be retried.

## Privacy

Everything in `--context` and `--fields` is sent to Google, and the free tier may use it to improve their products. Do not send names, phone numbers, ID numbers or other people's details.
