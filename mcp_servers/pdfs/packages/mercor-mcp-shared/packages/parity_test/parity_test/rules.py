"""Comparison rules and the three field classes (spec §2).

Extends the legacy ``parity.ComparisonRules`` (which had only ``ignore_paths``)
with the redesign's field-class model. Every path list below is matched with
``**`` globstar semantics (see :mod:`parity_test.paths`).

Field classes:

- ``ignore``     — drop entirely; no presence/type check. Volatile leaves.
- ``type_only``  — assert present + correct type, waive the value. Persisted as
                   a **typed placeholder** in the snapshot (see :mod:`sanitize`).
- ``reference``  — the id class; canonicalize through the identity map and
                   compare **tokens**, not values (see :mod:`identity`).
- (default)      — full value compare.
- escape-to-code — not a rule; waive a field here and assert the invariant in
                   Python against the returned response (spec §2).

Rules **layer**: a shared default set (from the control-plane config / the
``get_default_rules`` hook) merged with per-callsite overrides. The merge is
**explicit and lossless** — path lists union, scalars override, no silent drop
of unknown keys (the bug the legacy TS ``mergeRules`` had; spec §2).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

_DEFAULT_IGNORE_HEADERS = frozenset(
    {"date", "server", "x-request-id", "content-length", "content-type"}
)


@dataclass
class McpRules:
    """MCP-specific comparison knobs (spec §6.1)."""

    validate_output_schema: bool = True
    """Validate ``structuredContent`` against the tool descriptor's
    ``outputSchema`` as a separate assertion from the body diff."""
    compare_on: str = "content"
    """Which payload is the body-diff surface: normalized ``content`` (default)
    or ``structuredContent``."""
    ignore_structured_wrappers: list[str] = field(default_factory=list)
    """Wrapper keys to unwrap before output-schema validation (e.g. ``["data"]``
    to accept the ``data.data`` double-wrap seen in some Zoho tools)."""


@dataclass
class ComparisonRules:
    """Knobs for :func:`parity_test.compare`. All path lists use ``**`` globstar
    matching."""

    # Field classes (spec §2)
    ignore: list[str] = field(default_factory=list)
    type_only: list[str] = field(default_factory=list)
    reference: list[str] = field(default_factory=list)

    # Status / headers (as legacy)
    expected_statuses: list[int] | None = None
    compare_headers: bool = False
    ignore_headers: set[str] = field(default_factory=lambda: set(_DEFAULT_IGNORE_HEADERS))

    # MCP
    mcp: McpRules = field(default_factory=McpRules)

    def merge(self, override: ComparisonRules | None) -> ComparisonRules:
        """Return ``self`` layered under ``override`` — losslessly.

        Path-list classes **union** (order-preserving, deduped); scalar/optional
        fields take the override when it sets them; ``ignore_headers`` unions;
        ``mcp`` merges field-by-field. Never drops a key from either side.
        """
        if override is None:
            return replace(self)
        return ComparisonRules(
            ignore=_union(self.ignore, override.ignore),
            type_only=_union(self.type_only, override.type_only),
            reference=_union(self.reference, override.reference),
            expected_statuses=(
                override.expected_statuses
                if override.expected_statuses is not None
                else self.expected_statuses
            ),
            compare_headers=self.compare_headers or override.compare_headers,
            ignore_headers=set(self.ignore_headers) | set(override.ignore_headers),
            mcp=_merge_mcp(self.mcp, override.mcp),
        )


def _union(a: list[str], b: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in (*a, *b):
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _merge_mcp(a: McpRules, b: McpRules) -> McpRules:
    # Override (``b``) wins on the scalar knobs; wrappers union.
    # TODO(spec §6.1): if distinguishing "override left this default" from
    # "override set it explicitly" ever matters, switch these scalars to
    # sentinel defaults so a default value can't silently clobber the base.
    return McpRules(
        validate_output_schema=b.validate_output_schema,
        compare_on=b.compare_on or a.compare_on,
        ignore_structured_wrappers=_union(
            a.ignore_structured_wrappers, b.ignore_structured_wrappers
        ),
    )
