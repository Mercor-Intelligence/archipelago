"""Tests for strip_json_fences helper."""

import pytest

from runner.evals.output_llm.utils.json_utils import strip_json_fences


def test_bare_json_unchanged():
    assert strip_json_fences('{"a": 1}') == '{"a": 1}'


def test_strips_json_fence():
    assert strip_json_fences('```json\n{"a": 1}\n```') == '{"a": 1}'


def test_strips_plain_fence():
    assert strip_json_fences('```\n{"a": 1}\n```') == '{"a": 1}'


def test_strips_fence_with_trailing_whitespace():
    assert strip_json_fences('```json\n{"a": 1}\n```  \n') == '{"a": 1}'


def test_handles_empty_string():
    assert strip_json_fences("") == ""


def test_handles_whitespace_only():
    assert strip_json_fences("   ") == ""


def test_no_fence_with_inner_backticks():
    raw = '{"code": "```bash\\nls\\n```"}'
    assert strip_json_fences(raw) == raw


def test_idempotent():
    fenced = '```json\n{"a": 1}\n```'
    once = strip_json_fences(fenced)
    twice = strip_json_fences(once)
    assert once == twice
