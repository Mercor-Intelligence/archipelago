"""Binding resolution — REST ↔ MCP mapping (spec §6.2/§6.3).

A *binding* is a way to call one operation. Every operation has a REST binding;
some also have an MCP binding. Resolving a binding answers: for this
``operationId`` (+ discriminant), what tool name / route do I call, and what
schema governs the result?

Two regimes (spec §6.2):

- **Regime A — OAS-bound/derived.** The mapping is *computable* from the spec
  via the unified ``@endpoint`` decorator. MCP is generated to match the OAS;
  the tool name may be parameterized (``get_{module}`` + an app-supplied
  ``expand`` token set — spec §6.6). This is Zoho's regime.
- **Regime B — sovereign/modeled.** The MCP was designed independently; the
  **tool descriptor is the source of truth**, OAS may be absent. We model what
  exists; no computed mapping.

**Unified-endpoint metadata is intrinsic — it replaces ``tool_map.json``.**
``mcp_unified_endpoint.register_all(...)`` returns a ``RegistrationReport`` whose
``.bindings: list[ToolBinding]`` is 1:1 (and in order) with the registered
``.tools``. Each ``ToolBinding`` = ``(tool_name, method, path, operation_id,
pinned)`` — ``pinned`` is ``{"module": "Leads"}`` for a fanned tool, ``{}`` for a
plain one; ``report.fanned_out()`` narrows to the expanded ones. There is **no
side file and no module-level registry mirror** — the app must hand us the
report from ``register_all``'s return value (wired via a hooks
``binding_sources()`` stage, spec §6.3). ``pinned`` maps directly onto a
:class:`Binding` ``discriminant``.

Fan-out is **MCP-side only**: the REST route stays single and templated
(``/{module}`` registered once), so a fanned operation has one REST binding and
N MCP bindings. The pinned token is dropped from the MCP ``inputSchema`` and
injected at call time (pin wins over any client value) — so replay must inject
it too, not pass it as an argument.

Current fan-out tier is **name+description only** (single token; per-value
*schema* overrides are not implemented upstream and aren't expressible in
Zoho's generic-body OAS) — so all fanned MCP tools of an operation share one
``output_schema`` and one ``oneOf`` resolution; the discriminant is what
distinguishes their coverage cells (spec §6.6), not the schema.

The resolver is **pluggable**, tried in priority order (spec §6.3):
unified-endpoint report → route annotation → authored overlay (``tool_map.json``
remains only as a legacy fallback for apps not on the unified endpoint). First
hit wins; misses fall through; nothing found = REST-only (Regime B tools are
registered directly by descriptor).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class Binding:
    """One callable projection of an operation."""

    operation_id: str
    kind: str  # "rest" | "mcp"
    # REST
    route: str | None = None
    method: str | None = None
    # MCP
    tool_name: str | None = None
    output_schema: dict[str, Any] | None = None  # descriptor outputSchema (spec §6.1)
    # discriminated fan-out (spec §6.6): concrete token binding for this cell
    discriminant: dict[str, Any] = field(default_factory=dict)
    regime: str = "A"  # "A" derived | "B" sovereign


class BindingSource(Protocol):
    """A pluggable resolver stage (spec §6.3)."""

    def bindings_for(self, operation_id: str) -> list[Binding]:
        """Return zero or more bindings; empty = this source doesn't know."""
        ...


class BindingResolver:
    """Chains :class:`BindingSource` stages in priority order (spec §6.3).

    TODO(spec §6.3): implement the ordered chain (unified-endpoint
    ``RegistrationReport.bindings`` → route annotation → authored overlay, with
    ``tool_map.json`` only as a legacy fallback), first-hit-wins per
    (operationId, discriminant), plus direct registration of Regime-B tools by
    descriptor. The unified-endpoint source maps each ``ToolBinding`` →
    :class:`Binding` (``pinned`` → ``discriminant``), so it already carries the
    ``get_{module}`` fan-out — no separate name expansion needed for Regime A.
    """

    def __init__(self, sources: list[BindingSource] | None = None):
        self.sources = sources or []

    def resolve(self, operation_id: str) -> list[Binding]:
        raise NotImplementedError("TODO(spec §6.3): ordered pluggable resolution")

    def from_registration_report(self, report: Any) -> list[Binding]:
        """Adapt a unified-endpoint ``RegistrationReport`` into bindings: one MCP
        :class:`Binding` per ``report.bindings`` entry (``pinned`` → discriminant)
        plus the single templated REST binding per operation (spec §6.2/§6.6).

        TODO(spec §6.3): map ``ToolBinding(tool_name, method, path, operation_id,
        pinned)`` → :class:`Binding`; derive the one REST binding per operationId
        from the shared templated ``path``.
        """
        raise NotImplementedError("TODO(spec §6.3): RegistrationReport → bindings")
