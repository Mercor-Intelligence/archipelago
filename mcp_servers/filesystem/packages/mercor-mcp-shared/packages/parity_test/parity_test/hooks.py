"""Extension seam (spec §1/§9).

The library ships app-neutral. Per-app behavior arrives through an optional
``parity_test_ext`` module the clone provides; :func:`parity_test.load_extensions`
imports it and calls ``register(Hooks)``. Everything an app customizes hangs off
this one surface so the copied-in package itself is never edited.

Hooks (all optional):

- ``default_rules() -> ComparisonRules`` — the app's baseline field classes,
  merged under any per-call/per-test override (spec §2).
- ``binding_sources() -> list[BindingSource]`` — extra/reordered resolver stages
  (spec §6.3), e.g. a Zoho unified-endpoint source ahead of ``tool_map.json``.
- ``credential_provider()`` — supplies capture-time credentials and the redaction
  set (spec §9); never consulted in replay.
- ``expand_tokens(operation_id) -> dict[str, list[str]]`` — the app-supplied token
  set for parameterized tool names / discriminated coverage (spec §6.6).
- ``oas_document() -> dict`` — the app's **standard OpenAPI 3.x document** as an
  in-memory dict (the OAS *resolution hook*). Every app already produces one —
  Teams/Atlassian/Google via a ``build_openapi()``-style function, Zoho by
  assembling its runtime spec — however it cobbles the baseline together is the
  app's concern; the framework only needs the resolved standard document. The
  ``oas_index`` fixture (or :meth:`OasIndex.from_hooks`) parses it and layers the
  standard, app-owned overlays (``links.json``, ``branches.json``) on top. Called
  once per session; keep it lazy so importing the server / building the spec isn't
  paid unless parity tests run.
- ``mcp_document() -> dict | list`` — the app's **MCP** surface, parallel to
  ``oas_document`` and consumed by :meth:`OasIndex.from_hooks` with
  ``which="mcp"``. Returns **either** a single OpenAPI-shaped document (one MCP
  server — a scaffold twin may return the same doc it returns from
  ``oas_document``) **or** a list of :class:`~parity_test.oas.McpServer`
  ``(url, document)`` pairs, one per payload-split server (Zoho exposes 1k+ tools
  across 9 servers). A list builds an :class:`~parity_test.oas.McpRouter` that
  routes each op to the first server whose spec declares it. Build the document
  from the live server with ``tools_to_openapi(from_fastmcp(app))`` so it stays in
  lock-step with the tools the server actually exposes.
- ``populate(fixture_dir) -> None`` — seed the clone DB from a fixture dir
  (spec §8) by running the app's *own* end-to-end populate lifecycle, the same
  process ``mise populate`` uses. Wire it with
  ``parity_test.defaults.script_populate(scripts.populate_engine.main)``. Consumed
  by the ``@populate_seed`` / ``@seed_from_snapshot`` decorators via
  :mod:`parity_test.seeding`.
- ``db_reset()`` — return the clone schema to its pre-test baseline (spec §8),
  e.g. drop/recreate + re-seed the neutral baseline. Optional: when ``None``,
  seeding skips the reset (fine for read-only givens whose entity clears itself).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any


class Hooks:
    """Mutable registry the app's ``register(Hooks)`` populates."""

    default_rules: Callable[[], Any] | None = None
    binding_sources: Callable[[], list] | None = None
    credential_provider: Callable[[], Any] | None = None
    expand_tokens: Callable[[str], dict] | None = None
    oas_document: Callable[[], dict] | None = None
    mcp_document: Callable[[], Any] | None = None
    populate: Callable[[Path], None] | None = None
    db_reset: Callable[[], None] | None = None

    @classmethod
    def get_default_rules(cls) -> Any:
        """Return the app default rules or an empty :class:`ComparisonRules`."""
        from .rules import ComparisonRules

        if cls.default_rules is not None:
            return cls.default_rules()
        return ComparisonRules()

    @classmethod
    def reset(cls) -> None:
        cls.default_rules = None
        cls.binding_sources = None
        cls.credential_provider = None
        cls.expand_tokens = None
        cls.oas_document = None
        cls.mcp_document = None
        cls.populate = None
        cls.db_reset = None
