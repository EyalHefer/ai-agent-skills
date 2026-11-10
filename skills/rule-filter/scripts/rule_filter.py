"""Split a JSON list of objects into kept and rejected items using keyword, number and date rules."""

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml

# \b is unreliable next to Hebrew and punctuation, so word edges are spelled out.
_LETTER = "a-z0-9א-ת"
_WORD_START = f"(?<![{_LETTER}])"
_WORD_END = f"(?![{_LETTER}])"


CONDITIONS = ("exclude", "above", "below", "older_than_days", "newer_than_days")


@dataclass
class Rule:
    name: str
    fields: list[str]
    pattern: re.Pattern | None = None
    above: float | None = None
    below: float | None = None
    older_than_days: float | None = None
    newer_than_days: float | None = None


def _word_regex(word: str) -> str:
    word = word.lower()
    core = re.escape(word.strip("*"))
    start = f"[{_LETTER}]*" if word.startswith("*") else _WORD_START
    end = f"[{_LETTER}]*" if word.endswith("*") else _WORD_END
    return start + core + end


def build_rules(config: dict) -> list[Rule]:
    """A rule without any condition, such as an empty exclude list, is left out."""
    rules = []
    for r in config["rules"]:
        unknown = set(r) - {"name", "fields", *CONDITIONS}
        if unknown:
            raise ValueError(f"rule {r.get('name')!r}: unknown keys {sorted(unknown)}")
        words = r.get("exclude") or []
        rule = Rule(r["name"], r["fields"],
                    pattern=re.compile("|".join(_word_regex(w) for w in words)) if words else None,
                    **{key: r.get(key) for key in CONDITIONS[1:]})
        if any(getattr(rule, key) is not None for key in ("pattern", *CONDITIONS[1:])):
            rules.append(rule)
    return rules


def _field_text(item: dict, field: str) -> str:
    value = item.get(field) or ""
    return "\n".join(map(str, value)) if isinstance(value, list) else str(value)


def _number(value) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _age_days(value, now: datetime) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (now - when).total_seconds() / 86400


def _show(number: float) -> str:
    return f"{number:g}"


def _match(rule: Rule, item: dict, now: datetime) -> str | None:
    """What made the rule match the item, or None. A field that is missing, or not a number or a date
    where one is needed, never matches."""
    for field in rule.fields:
        if rule.pattern and (m := rule.pattern.search(_field_text(item, field).lower())):
            return m.group()
        number = _number(item.get(field))
        if number is not None:
            if rule.above is not None and number > rule.above:
                return f"{field} {_show(number)} > {_show(rule.above)}"
            if rule.below is not None and number < rule.below:
                return f"{field} {_show(number)} < {_show(rule.below)}"
        age = _age_days(item.get(field), now)
        if age is not None:
            if rule.older_than_days is not None and age > rule.older_than_days:
                return f"{field} {age:.0f} days ago, more than {_show(rule.older_than_days)}"
            if rule.newer_than_days is not None and age < rule.newer_than_days:
                return f"{field} {age:.0f} days ago, less than {_show(rule.newer_than_days)}"
    return None


def apply_rules(items: list[dict], rules: list[Rule], now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    kept, rejected = [], []
    for item in items:
        for rule in rules:
            matched = _match(rule, item, now)
            if matched is not None:
                rejected.append({"item": item, "rule": rule.name, "matched": matched})
                break
        else:
            kept.append(item)
    return {"kept": kept, "rejected": rejected}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("rules_file", type=Path)
    args = parser.parse_args()

    rules = build_rules(yaml.safe_load(args.rules_file.read_text(encoding="utf-8")))
    items = json.loads(sys.stdin.buffer.read().decode("utf-8-sig"))
    sys.stdout.reconfigure(encoding="utf-8")
    json.dump(apply_rules(items, rules), sys.stdout, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
