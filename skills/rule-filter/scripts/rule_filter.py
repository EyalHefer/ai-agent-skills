"""Split a JSON list of objects into kept and rejected items using keyword rules."""

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

# \b is unreliable next to Hebrew and punctuation, so word edges are spelled out.
_LETTER = "a-z0-9א-ת"
_WORD_START = f"(?<![{_LETTER}])"
_WORD_END = f"(?![{_LETTER}])"


@dataclass
class Rule:
    name: str
    fields: list[str]
    pattern: re.Pattern


def _word_regex(word: str) -> str:
    word = word.lower()
    core = re.escape(word.strip("*"))
    start = f"[{_LETTER}]*" if word.startswith("*") else _WORD_START
    end = f"[{_LETTER}]*" if word.endswith("*") else _WORD_END
    return start + core + end


def build_rules(config: dict) -> list[Rule]:
    return [Rule(r["name"], r["fields"], re.compile("|".join(_word_regex(w) for w in r["exclude"])))
            for r in config["rules"] if r["exclude"]]


def _field_text(item: dict, field: str) -> str:
    value = item.get(field) or ""
    return "\n".join(map(str, value)) if isinstance(value, list) else str(value)


def apply_rules(items: list[dict], rules: list[Rule]) -> dict:
    kept, rejected = [], []
    for item in items:
        for rule in rules:
            match = next((m for field in rule.fields
                          if (m := rule.pattern.search(_field_text(item, field).lower()))), None)
            if match:
                rejected.append({"item": item, "rule": rule.name, "matched": match.group()})
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
