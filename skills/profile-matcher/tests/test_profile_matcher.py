import json
import sys
from pathlib import Path

import jsonschema

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL / "scripts"))

from profile_matcher import MAX_FIELD_CHARS, ModelsBusy, build_prompt, rank  # noqa: E402

FIELDS = ["title", "text"]


def score(id, value):
    return {"id": id, "score": value, "confidence": "high", "why": "reason", "gaps": ""}


def answer(*rows):
    return lambda prompt: json.dumps(list(rows))


def items(count):
    return [{"title": f"job {i}"} for i in range(count)]


def test_results_are_sorted_best_first():
    result = rank(items(3), "ctx", "ins", answer(score(0, 10), score(1, 90), score(2, 50)), FIELDS)
    assert [s["score"] for s in result["scored"]] == [90, 50, 10]
    assert result["scored"][0]["item"] == {"title": "job 1"}


def test_items_are_sent_in_batches():
    prompts = []

    def generate(prompt):
        prompts.append(prompt)
        return json.dumps([score(i, 50) for i in range(prompt.count("## Item id="))])

    result = rank(items(5), "ctx", "ins", generate, FIELDS, batch_size=2)
    assert len(prompts) == 3
    assert len(result["scored"]) == 5


def test_item_the_model_skipped_is_unscored():
    result = rank(items(2), "ctx", "ins", answer(score(0, 70)), FIELDS)
    assert result["unscored"] == [{"title": "job 1"}]


def test_unknown_id_is_ignored():
    result = rank(items(1), "ctx", "ins", answer(score(0, 70), score(9, 99)), FIELDS)
    assert len(result["scored"]) == 1


def test_busy_models_fail_one_batch_and_keep_the_rest():
    calls = []

    def generate(prompt):
        calls.append(prompt)
        if len(calls) == 1:
            raise ModelsBusy
        return json.dumps([score(0, 80)])

    result = rank(items(2), "ctx", "ins", generate, FIELDS, batch_size=1)
    assert len(result["unscored"]) == 1
    assert len(result["scored"]) == 1


def test_invalid_answers_leave_the_batch_unscored():
    for bad in ("not json", json.dumps([score(0, 500)])):
        result = rank(items(1), "ctx", "ins", lambda prompt: bad, FIELDS)
        assert result["scored"] == []
        assert len(result["unscored"]) == 1


def test_prompt_has_context_and_limits_field_size():
    prompt = build_prompt("my profile", "my rules", [{"title": "t", "text": "x" * 5000}], FIELDS)
    assert "my profile" in prompt and "my rules" in prompt
    assert "x" * MAX_FIELD_CHARS in prompt
    assert "x" * (MAX_FIELD_CHARS + 1) not in prompt


def test_field_size_limit_can_be_raised():
    prompt = build_prompt("ctx", "ins", [{"title": "t", "text": "x" * 5000}], FIELDS, max_chars=4000)
    assert "x" * 4000 in prompt
    assert "x" * 4001 not in prompt


def test_output_matches_schema():
    result = rank(items(2), "ctx", "ins", answer(score(0, 70)), FIELDS)
    schema = json.loads((SKILL / "schema.json").read_text(encoding="utf-8"))
    jsonschema.validate(result, schema["output"])
