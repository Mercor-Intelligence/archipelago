"""Typed parity mismatches (extends the legacy ``parity.failures`` set).

Every comparison reports **all** divergences at once as a list of
:class:`ParityMismatch`; :class:`ParityFailure` (an ``AssertionError`` subclass)
aggregates them so pytest fails while an agent can still tell contract drift
from a hand-authored assert.

Beyond the legacy categories (status/value/type/missing/extra/length/header/
mcp-error) this module adds the categories the redesign introduces:

- :class:`ContractSchemaMismatch` — a response didn't resolve to any documented
  OAS body / MCP ``outputSchema`` branch, or resolved ambiguously (spec §4).
- :class:`CoverageDrift` — the replayed coverage sequence diverged from the
  captured one (spec §5/§6.6).
- :class:`UnattributableId` — an id-shaped value that no fixture minted and no
  link covers (spec §3).
- :class:`OutputSchemaMismatch` — MCP ``structuredContent`` failed validation
  against the tool descriptor's ``outputSchema`` (spec §6.1).
"""

from __future__ import annotations

from typing import Any


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


class ParityMismatch:
    """Base for a single located divergence.

    ``describe()`` renders a terse, side-explicit line. The two sides are always
    named: ``snapshot`` is the recorded reference contract, ``clone`` is what the
    system under test just produced. A mismatch means the clone diverged from
    that contract — the clone is the suspect, not the snapshot. Subclasses
    override :meth:`_detail` for category-specific phrasing; the base wording is
    the neutral fallback used by non-comparison sentinels (e.g. a missing
    snapshot), which aren't a snapshot-vs-clone diff.
    """

    category = "ParityMismatch"

    def __init__(self, path: str = "", *, expected: Any = None, actual: Any = None):
        self.path = path
        self.expected = expected
        self.actual = actual

    def _detail(self) -> str:
        return f"expected {self.expected!r}, actual {self.actual!r}"

    def describe(self) -> str:
        loc = f"{self.path}: " if self.path else ""
        return f"{loc}{self._detail()}  [{self.category}]"


class ResponseCodeMismatch(ParityMismatch):
    category = "ResponseCodeMismatch"

    def _detail(self) -> str:
        return f"snapshot={self.expected}, clone={self.actual}"


class PropertyValueMismatch(ParityMismatch):
    category = "PropertyValueMismatch"

    def _detail(self) -> str:
        return f"snapshot={self.expected!r}, clone={self.actual!r}"


class TypeMismatch(ParityMismatch):
    category = "TypeMismatch"

    def _detail(self) -> str:
        return (
            f"snapshot={_json_type(self.expected)}({self.expected!r}), "
            f"clone={_json_type(self.actual)}({self.actual!r})"
        )


class MissingProperty(ParityMismatch):
    category = "MissingProperty"

    def _detail(self) -> str:
        return f"snapshot={self.expected!r}, clone=<absent>"


class ExtraProperty(ParityMismatch):
    category = "ExtraProperty"

    def _detail(self) -> str:
        return f"snapshot=<absent>, clone={self.actual!r}"


class LengthMismatch(ParityMismatch):
    category = "LengthMismatch"

    def _detail(self) -> str:
        return f"snapshot len={self.expected}, clone len={self.actual}"


class HeaderMismatch(ParityMismatch):
    category = "HeaderMismatch"

    def __init__(self, name: str, *, expected: Any = None, actual: Any = None):
        super().__init__(name, expected=expected, actual=actual)
        self.name = name

    def _detail(self) -> str:
        return f"snapshot={self.expected!r}, clone={self.actual!r}"


class McpErrorMismatch(ParityMismatch):
    category = "McpErrorMismatch"

    def _detail(self) -> str:
        return f"snapshot isError={self.expected}, clone isError={self.actual}"


# --- New categories (redesign) ----------------------------------------------


class ContractSchemaMismatch(ParityMismatch):
    """No documented body/`oneOf` branch matched, or >1 matched (spec §4)."""

    category = "ContractSchemaMismatch"


class OutputSchemaMismatch(ParityMismatch):
    """MCP ``structuredContent`` ⟂ descriptor ``outputSchema`` (spec §6.1)."""

    category = "OutputSchemaMismatch"


class CoverageDrift(ParityMismatch):
    """Replayed coverage sequence ≠ captured sequence (spec §5/§6.6)."""

    category = "CoverageDrift"


class UnattributableId(ParityMismatch):
    """Id-shaped value with no minting fixture and no covering link (spec §3)."""

    category = "UnattributableId"


class ParityFailure(AssertionError):  # noqa: N818 — public assertion name; a pytest-facing failure, not an "Error"
    """Aggregates every :class:`ParityMismatch` from one comparison."""

    def __init__(self, mismatches: list[ParityMismatch], context: str = ""):
        self.mismatches = mismatches
        self.context = context
        count = len(mismatches)
        header = f"{count} parity mismatch{'' if count == 1 else 'es'}"
        if context:
            header += f" in {context}"
        body = "\n".join(f"  - {m.describe()}" for m in mismatches)
        super().__init__(f"{header}:\n{body}")


class StaleSnapshotFailure(AssertionError):  # noqa: N818 — pytest-facing failure, not an "Error"
    """The test's current inputs diverged from the committed snapshot's inputs.

    A snapshot records a fingerprint of the (tokenized) inputs it was captured
    with. When a later run passes *different* inputs — the test body changed but
    the snapshot was never re-captured — replay would silently verify the clone
    against a snapshot recorded for a **different request**. That is never a real
    parity signal: the baseline is stale, and the fix is a re-capture, not a
    clone change. So replay raises this instead of serving the stale artifact
    (spec §7). Capture mode auto-re-captures on the same condition; spoof warns.

    The message names the fix (re-capture) so nobody burns time treating it as a
    contract mismatch.
    """

    def __init__(self, context: str, *, expected: str, actual: str):
        self.context = context
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"stale snapshot in {context}: inputs changed since capture "
            f"(snapshot fingerprint={expected}, current={actual}). "
            "Re-capture this test (capture mode, or PARITY_FULL_REFRESH=1) — "
            "the committed snapshot was recorded for different inputs."
        )
