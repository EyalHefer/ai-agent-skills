---
name: rule-filter
description: Remove items from a JSON list using keyword, number and date rules, before any LLM call. Use to drop clearly irrelevant items cheaply. Works on any list of objects, such as job postings or GitHub issues.
---

# Rule Filter

Takes a JSON array of objects and splits it into kept and rejected items, with the reason for each rejection. An item is rejected when any rule matches.

## Run

```
python scripts/rule_filter.py <rules-file> < items.json
```

Writes a JSON object to stdout. The format is defined in [schema.json](schema.json).

## Rules file

YAML or JSON. Other top-level keys are ignored, so the rules can live in a larger settings file.

```yaml
rules:
  - name: seniority           # shown in the rejection reason
    fields: [title]           # item fields to search
    exclude: [senior, lead, "recruit*", "*שיווק"]
```

## Matching

- Case-insensitive, whole words: `lead` matches "Team Lead", not "Leading".
- `*` at the end or start of a word allows extra letters there: `recruit*` also matches "Recruiter", and `*שיווק` also matches "לשיווק".
- A field holding a list, such as GitHub labels, is searched item by item.
- A missing field is treated as empty.

## Number and date rules

A rule can also compare a field with a number or a date, instead of or as well as `exclude`:

```yaml
rules:
  - name: busy
    fields: [comments]
    above: 20                 # also: below
  - name: stale
    fields: [updated_at]
    older_than_days: 180      # also: newer_than_days. ISO dates, such as GitHub's
```

`matched` then says what was found, such as `comments 25 > 20`. A field that is missing, or is not a number or a date where one is needed, never matches, so an item is not dropped for missing data. A rule with a key it does not know fails, so a typo does not silently do nothing.
