"""Response comparison over the field-class model (spec §2).

Drives a structural deep-diff from :class:`~parity_test.rules.ComparisonRules`
field classes. The key move: **sanitize the SUT (actual) body with the exact
same field-class transform that produced the snapshot** (:func:`sanitize.
sanitize_response`), then diff the two sanitized bodies. Symmetric transforms
mean ``ignore`` drops from both, ``type_only`` reduces both to the same typed
placeholder, and ``reference`` tokenizes both — so the diff only surfaces
genuine divergence.

Pipeline for a body diff:

1. Sanitize ``actual`` under ``rules`` (ids tokenized via the shared
   :class:`~parity_test.identity.IdRegistry`) — the snapshot side was sanitized
   at capture.
2. Deep-diff sanitized-actual against ``snap.body``; **expected = snapshot,
   actual = SUT** in every :class:`ParityMismatch`.

Dispatches on ``snap.binding``: REST compares status + body; MCP compares
``isError`` + normalized body (``structuredContent`` vs ``outputSchema`` is a
separate assertion, deferred).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .failures import (
    ExtraProperty,
    LengthMismatch,
    McpErrorMismatch,
    MissingProperty,
    ParityFailure,
    ParityMismatch,
    PropertyValueMismatch,
    ResponseCodeMismatch,
    TypeMismatch,
)
from .identity import IdRegistry
from .rules import ComparisonRules
from .sanitize import sanitize_response
from .snapshot import Snapshot


@dataclass
class ComparisonResult:
    mismatches: list[ParityMismatch] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.mismatches

    def summary(self) -> str:
        return "OK" if self.passed else "\n".join(m.describe() for m in self.mismatches)

    def raise_for_failure(self, context: str = "") -> None:
        if self.mismatches:
            raise ParityFailure(self.mismatches, context=context)


def _fmt(path: tuple[Any, ...]) -> str:
    out = ""
    for seg in path:
        out += f"[{seg}]" if isinstance(seg, int) else (f".{seg}" if out else str(seg))
    return out or "<root>"


def _json_type(v: Any) -> str:
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, (int, float)):
        return "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "array"
    if isinstance(v, dict):
        return "object"
    return "null"


def _diff(expected: Any, actual: Any, path: tuple[Any, ...], out: list[ParityMismatch]) -> None:
    if _json_type(expected) != _json_type(actual):
        # Pass the raw values; TypeMismatch renders type + value (e.g.
        # ``snapshot=string('5'), clone=number(5)``).
        out.append(TypeMismatch(_fmt(path), expected=expected, actual=actual))
        return
    if isinstance(expected, dict):
        for key in expected:
            if key not in actual:
                out.append(MissingProperty(_fmt((*path, key)), expected=expected[key], actual=None))
            else:
                _diff(expected[key], actual[key], (*path, key), out)
        for key in actual:
            if key not in expected:
                out.append(ExtraProperty(_fmt((*path, key)), expected=None, actual=actual[key]))
        return
    if isinstance(expected, list):
        if len(expected) != len(actual):
            out.append(LengthMismatch(_fmt(path), expected=len(expected), actual=len(actual)))
        for i in range(min(len(expected), len(actual))):
            _diff(expected[i], actual[i], (*path, i), out)
        return
    if expected != actual:
        out.append(PropertyValueMismatch(_fmt(path), expected=expected, actual=actual))


def compare(
    actual: Any,
    snap: Snapshot,
    rules: ComparisonRules | None = None,
    *,
    ids: IdRegistry | None = None,
) -> ComparisonResult:
    """Diff a replay result against its snapshot; never raises. ``rules`` is the
    already-resolved rule set (the harness merges defaults + overrides)."""
    rules = rules or ComparisonRules()
    ids = ids or IdRegistry()
    mismatches: list[ParityMismatch] = []

    if snap.binding == "mcp":
        act_error = bool(getattr(actual, "is_error", False))
        if act_error != snap.is_error:
            mismatches.append(McpErrorMismatch("isError", expected=snap.is_error, actual=act_error))
        actual_body = getattr(actual, "content", None)
    else:
        act_status = getattr(actual, "status", None)
        if act_status != snap.status:
            mismatches.append(
                ResponseCodeMismatch("status", expected=snap.status, actual=act_status)
            )
        actual_body = getattr(actual, "body", None)

    clean_actual = sanitize_response(actual_body, rules, tokenize=ids.token_for)
    _diff(snap.body, clean_actual, (), mismatches)

    return ComparisonResult(mismatches)


def assert_matches(
    actual: Any,
    snap: Snapshot,
    rules: ComparisonRules | None = None,
    *,
    ids: IdRegistry | None = None,
) -> None:
    """Raises :class:`ParityFailure` on mismatch. Most tests never call this
    directly — ``api.call``/``api.tool`` verify as a side effect."""
    compare(actual, snap, rules, ids=ids).raise_for_failure(
        context=f"{snap.label or snap.operation_id or '?'} (clone vs reference snapshot)"
    )
