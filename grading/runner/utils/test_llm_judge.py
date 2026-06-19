"""Tests for JSON payload extraction in the shared LLM judge utilities.

Regression coverage for issue #97: a valid-but-fenced judge response should
parse without exhausting MAX_JSON_RETRIES.
"""

import json

import pytest

from runner.utils.llm_judge import _first_balanced_json, extract_json_payload


def _loads(text: str) -> dict:
    return json.loads(extract_json_payload(text))


def test_plain_json_passes_through():
    payload = '{"result": 1, "reason": "ok"}'
    assert extract_json_payload(payload) == payload
    assert _loads(payload) == {"result": 1, "reason": "ok"}


def test_strips_json_fence():
    fenced = '```json\n{"result": 0, "reason": "missing citation"}\n```'
    assert _loads(fenced) == {"result": 0, "reason": "missing citation"}


def test_strips_bare_fence():
    fenced = '```\n{"result": 1, "reason": "ok"}\n```'
    assert _loads(fenced) == {"result": 1, "reason": "ok"}


def test_handles_preamble_before_json():
    noisy = 'Here is my assessment:\n{"result": 1, "reason": "complete"}'
    assert _loads(noisy) == {"result": 1, "reason": "complete"}


def test_fence_with_preamble_and_trailing_text():
    noisy = (
        "Sure, here you go:\n"
        '```json\n{"result": 0, "reason": "wrong figure"}\n```\n'
        "Let me know if you need more."
    )
    assert _loads(noisy) == {"result": 0, "reason": "wrong figure"}


def test_ignores_braces_inside_strings():
    payload = '{"result": 1, "reason": "uses a {placeholder} token"}'
    assert _loads(payload) == {"result": 1, "reason": "uses a {placeholder} token"}


def test_valid_json_with_backticks_in_field_is_not_mangled():
    # A well-formed payload whose string field contains a pair of triple
    # backtick segments must parse verbatim -- fence stripping must not run
    # before the as-is parse check (regression for the fence-strip bug).
    payload = json.dumps(
        {"result": 1, "reason": "use ```json\n{}\n``` for output"}
    )
    assert extract_json_payload(payload) == payload
    assert _loads(payload)["reason"] == "use ```json\n{}\n``` for output"


def test_skips_balanced_preamble_braces_before_real_object():
    # Harmless balanced braces in the preamble must not stop extraction; the
    # later valid object should still be found.
    noisy = 'Note: {see appendix} below\n{"result": 0, "reason": "x"}'
    assert _loads(noisy) == {"result": 0, "reason": "x"}


def test_first_balanced_json_skips_unparseable_block():
    text = '{not: valid} then {"result": 1, "reason": "y"}'
    assert _first_balanced_json(text) == '{"result": 1, "reason": "y"}'


def test_first_balanced_json_returns_none_for_garbage():
    assert _first_balanced_json("no json here at all") is None


def test_unparseable_input_returned_unchanged():
    # No cleaner payload found -> original returned so existing retry logic runs.
    garbage = "totally not json"
    assert extract_json_payload(garbage) == garbage


@pytest.mark.parametrize("empty", ["", None])
def test_empty_input_is_safe(empty):
    assert extract_json_payload(empty) == empty
